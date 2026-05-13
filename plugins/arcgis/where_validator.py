"""Input validators for ArcGIS Feature Service queries.

Hardens SQL WHERE clauses, out_fields lists, and order_by expressions
against injection before they are forwarded to an ArcGIS REST endpoint.
"""

import difflib
import re
from typing import Iterable, Optional


class WhereValidator:
    """Validates WHERE clause strings for Feature Service queries."""

    FORBIDDEN_KEYWORDS = [
        "INSERT",
        "UPDATE",
        "DELETE",
        "DROP",
        "TRUNCATE",
        "ALTER",
        "CREATE",
        "EXEC",
        "EXECUTE",
        "UNION",
        "SELECT",
        "FROM",
        "JOIN",
        "INTO",
        "EXISTS",
        "MERGE",
        "GRANT",
        "REVOKE",
        "DECLARE",
        "SLEEP",
        "WAITFOR",
        "BENCHMARK",
    ]

    FORBIDDEN_SUBSTRINGS = [
        ";",
        "--",
        "/*",
        "*/",
        "@@",
        "xp_",
        "sp_",
        "0x",
    ]

    MAX_LENGTH = 2000

    @classmethod
    def validate(cls, where: str) -> str:
        """Validate and sanitize a WHERE clause string.

        Args:
            where: SQL WHERE clause string

        Returns:
            The original WHERE clause if valid, or "1=1" if empty/None

        Raises:
            ValueError: If the clause contains forbidden SQL keywords or
                suspicious substrings (stacked queries, comments, etc.).
        """
        if not where:
            return "1=1"

        where = where.strip()
        if not where:
            return "1=1"

        if len(where) > cls.MAX_LENGTH:
            raise ValueError(
                f"WHERE clause exceeds max length ({cls.MAX_LENGTH} chars)"
            )

        lowered = where.lower()
        for bad in cls.FORBIDDEN_SUBSTRINGS:
            if bad.lower() in lowered:
                raise ValueError(
                    f"Forbidden substring {bad!r} detected in WHERE clause"
                )

        where_upper = where.upper()
        for keyword in cls.FORBIDDEN_KEYWORDS:
            if re.search(rf"\b{keyword}\b", where_upper):
                raise ValueError(
                    f"Forbidden keyword '{keyword}' detected in WHERE clause"
                )

        return where

    # Reserved SQL/Esri tokens that look like identifiers but aren't
    # field references. Anything outside this set that survives literal
    # stripping is treated as a candidate field name and checked against
    # the layer schema in ``validate_against_schema``.
    SQL_RESERVED = frozenset(
        {
            "AND", "OR", "NOT", "BETWEEN", "IN", "LIKE", "ESCAPE", "IS",
            "NULL", "TRUE", "FALSE",
            "DATE", "TIMESTAMP", "TIME",
            "CURRENT_DATE", "CURRENT_TIMESTAMP",
            "YEAR", "MONTH", "DAY", "HOUR", "MINUTE", "SECOND",
            "CASE", "WHEN", "THEN", "ELSE", "END",
            "UPPER", "LOWER", "TRIM", "LTRIM", "RTRIM",
            "LENGTH", "LEN", "SUBSTRING", "SUBSTR",
            "CHARINDEX", "POSITION", "COALESCE", "NULLIF",
            "CAST", "AS", "EXTRACT", "TO_DATE", "TO_TIMESTAMP",
            "ABS", "ROUND", "CEIL", "CEILING", "FLOOR",
            "MIN", "MAX", "SUM", "AVG", "COUNT", "STDDEV",
            "ANY", "ALL", "SOME", "DISTINCT",
        }
    )

    _STRING_LITERAL_RE = re.compile(r"'(?:[^']|'')*'")
    _NUM_LITERAL_RE = re.compile(r"\b\d+(?:\.\d+)?\b")
    _IDENT_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\b")

    @classmethod
    def validate_against_schema(
        cls,
        where: str,
        allowed_fields: Optional[Iterable[str]],
    ) -> None:
        """Verify every identifier in ``where`` is a real layer field.

        Catches typo'd field names -- a hallucination magnet for LLMs --
        before they hit ArcGIS, which would otherwise return a cryptic
        ``Unable to perform query`` error. On unknown identifiers we
        raise with a ``difflib`` suggestion so the caller (often a
        model) can self-correct.

        Args:
            where: A WHERE clause that has already passed
                ``WhereValidator.validate``.
            allowed_fields: Field names from the layer schema. Field
                names in ArcGIS are case-sensitive. Pass ``None`` /
                empty to skip the check (e.g. when the schema fetch
                failed -- graceful degradation).

        Raises:
            ValueError: When the WHERE references an identifier that
                isn't in ``allowed_fields`` and isn't a SQL keyword or
                function from ``SQL_RESERVED``.
        """
        if not where:
            return
        stripped_where = where.strip()
        if not stripped_where or stripped_where == "1=1":
            return
        if not allowed_fields:
            return

        allowed_set = {f for f in allowed_fields if f}
        if not allowed_set:
            return

        # Drop string literals first so values like 'Park' don't get
        # mis-tokenized as field names. Then strip numeric literals.
        no_strings = cls._STRING_LITERAL_RE.sub("", where)
        no_numbers = cls._NUM_LITERAL_RE.sub("", no_strings)
        candidates = set(cls._IDENT_RE.findall(no_numbers))
        candidates = {
            c for c in candidates if c.upper() not in cls.SQL_RESERVED
        }
        unknown = sorted(c for c in candidates if c not in allowed_set)
        if not unknown:
            return

        sorted_allowed = sorted(allowed_set)
        parts = []
        for u in unknown:
            suggestions = difflib.get_close_matches(
                u, sorted_allowed, n=1, cutoff=0.6
            )
            if suggestions:
                parts.append(
                    f"Field {u!r} not found in this layer -- did you "
                    f"mean {suggestions[0]!r}? (Field names are "
                    f"case-sensitive.)"
                )
            else:
                parts.append(
                    f"Field {u!r} not found in this layer."
                )
        parts.append(
            "Call get_layer_schema to see all available field names."
        )
        raise ValueError(" ".join(parts))


class OutFieldsValidator:
    """Validates an ArcGIS ``outFields`` parameter.

    Accepts ``*`` or a comma-separated list of bare field identifiers.
    Rejects anything with whitespace, operators, or non-identifier chars
    to keep this surface from becoming an injection sink.
    """

    _IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
    MAX_FIELDS = 100

    @classmethod
    def validate(cls, out_fields: str) -> str:
        if out_fields is None:
            return "*"
        value = out_fields.strip()
        if not value or value == "*":
            return "*"

        parts = [p.strip() for p in value.split(",")]
        if len(parts) > cls.MAX_FIELDS:
            raise ValueError(
                f"out_fields exceeds max of {cls.MAX_FIELDS} fields"
            )
        for part in parts:
            if not cls._IDENT.match(part):
                raise ValueError(
                    f"Invalid field name in out_fields: {part!r}"
                )
        return ",".join(parts)


class OrderByValidator:
    """Validates an ArcGIS ``orderByFields`` parameter.

    Accepts one or more comma-separated ``<field>[ ASC|DESC]`` entries.
    """

    _ENTRY = re.compile(
        r"^[A-Za-z_][A-Za-z0-9_]*(\s+(ASC|DESC))?$",
        re.IGNORECASE,
    )
    MAX_FIELDS = 10

    @classmethod
    def validate(cls, order_by: str) -> str:
        if not order_by:
            return ""
        value = order_by.strip()
        if not value:
            return ""

        parts = [p.strip() for p in value.split(",")]
        if len(parts) > cls.MAX_FIELDS:
            raise ValueError(
                f"order_by exceeds max of {cls.MAX_FIELDS} fields"
            )
        for part in parts:
            if not cls._ENTRY.match(part):
                raise ValueError(f"Invalid order_by entry: {part!r}")
        return ",".join(parts)
