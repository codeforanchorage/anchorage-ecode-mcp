"""Input validators for ArcGIS Feature Service queries.

Hardens SQL WHERE clauses, out_fields lists, and order_by expressions
against injection before they are forwarded to an ArcGIS REST endpoint.
"""

import re


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
