"""search_code guidance for attorney users (1.4.0).

Two behaviours, both driven by prod evidence from the first attorney user
(Nov 3-13, 2026): 49 of 128 non-empty searches returned >= 100 hits because
eCode360 ORs side-by-side terms, and 12 of 139 returned nothing with no
explanation -- twice the user gave up after one.

The server never rewrites a query. It only ATTACHES caveats, so every case
here also checks that the query echoed back is the one the caller sent and
that the result still conforms to the advertised schema.
"""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from jsonschema import validate

from plugins.ecode.config_schema import EcodePluginConfig
from plugins.ecode.plugin import (
    CAVEAT_MORE_PAGES,
    CAVEAT_NO_MATCH_IN_CODE,
    CAVEAT_PAGE_PAST_END,
    CAVEAT_STATE_LAW_NAMED,
    CAVEAT_TERMS_OR_COMBINED,
    OR_CAVEAT_MIN_TOTAL,
    SEARCH_OUTPUT_SCHEMA,
    EcodePlugin,
)

NOTICE_QUERY = (
    '"notice" "claim" "within" "days after" injury '
    '"municipality shall not be liable"'
)


@pytest.fixture
def plugin():
    cfg = {
        "enabled": True,
        "base_url": "https://api.ecode360.com",
        "customer_id": "AN6998",
        "city_name": "Municipality of Anchorage",
        "timeout": 20,
    }
    p = EcodePlugin(cfg)
    p.plugin_config = EcodePluginConfig(**cfg)
    return p


def _wire(plugin, payload):
    plugin.client = MagicMock()
    plugin.client.get = AsyncMock(
        return_value=httpx.Response(
            200,
            json=payload,
            request=httpx.Request("GET", "https://api.ecode360.com/v1/x"),
        )
    )


def _hits(n):
    return [
        {"id": str(49400000 + i), "number": f"1.{i:02d}.010",
         "title": "T", "text": "snippet", "type": "section"}
        for i in range(n)
    ]


def _page(query, total, n_rows=20):
    return {
        "results": _hits(n_rows if total else 0),
        "query": query,
        "page": "0",
        "maxPages": max(1, -(-total // 20)),
        "totalResultCount": total,
    }


async def _search(plugin, query, total, page=0, payload=None):
    _wire(plugin, payload or _page(query, total))
    result = await plugin.execute_tool(
        "search_code", {"query": query, "page": page}
    )
    assert result.success, result.error_message
    sc = result.structured_content
    validate(sc, SEARCH_OUTPUT_SCHEMA)
    # The upstream was asked for exactly what the caller wrote.
    sent = plugin.client.get.call_args.kwargs["params"]["query"]
    assert sent == query, "the server rewrote the caller's query"
    return sc, result.content[0]["text"]


def _codes(sc):
    return [c["code"] for c in sc["caveats"]]


# --------------------------------------------------------------------------
# FIX 1 -- terms_or_combined
# --------------------------------------------------------------------------


class TestTermsOrCombined:
    @pytest.mark.asyncio
    async def test_three_bare_terms_over_threshold(self, plugin):
        q = "transportation right-of-way district"
        sc, text = await _search(plugin, q, 803)
        assert CAVEAT_TERMS_OR_COMBINED in _codes(sc)
        msg = next(
            c["message"] for c in sc["caveats"]
            if c["code"] == CAVEAT_TERMS_OR_COMBINED
        )
        assert "803 sections matched" in msg
        # The suggestion is the caller's first two terms, in their order.
        assert "e.g. transportation AND right-of-way, then add" in msg
        assert "AND district" not in msg
        assert msg in text, "caveat missing from the human-readable text"

    @pytest.mark.asyncio
    async def test_same_query_with_and_gets_no_caveat(self, plugin):
        q = "transportation AND right-of-way AND district"
        sc, _ = await _search(plugin, q, 803)
        assert CAVEAT_TERMS_OR_COMBINED not in _codes(sc)

    @pytest.mark.asyncio
    async def test_attorney_six_term_query(self, plugin):
        sc, _ = await _search(plugin, NOTICE_QUERY, 1743)
        msg = next(
            c["message"] for c in sc["caveats"]
            if c["code"] == CAVEAT_TERMS_OR_COMBINED
        )
        # ANDing all six returns 0 live; the first two return 25 with
        # § 1.50.010 near the top.
        assert 'e.g. "notice" AND "claim", then add more terms' in msg
        assert '"within"' not in msg
        # Still a normal page of results with the usual paging caveat.
        assert CAVEAT_MORE_PAGES in _codes(sc)
        assert len(sc["rows"]) == 20

    @pytest.mark.asyncio
    async def test_two_terms_qualify(self, plugin):
        sc, _ = await _search(plugin, '"sidewalk" "snow"', 250)
        msg = next(
            c["message"] for c in sc["caveats"]
            if c["code"] == CAVEAT_TERMS_OR_COMBINED
        )
        # Two terms: the suggestion is the whole query, with no "add more".
        assert 'e.g. "sidewalk" AND "snow".' in msg
        assert "add more terms" not in msg

    @pytest.mark.asyncio
    async def test_below_threshold_no_caveat(self, plugin):
        sc, _ = await _search(
            plugin, '"sidewalk" "snow"', OR_CAVEAT_MIN_TOTAL - 1
        )
        assert CAVEAT_TERMS_OR_COMBINED not in _codes(sc)

    @pytest.mark.asyncio
    async def test_at_threshold_caveat(self, plugin):
        sc, _ = await _search(
            plugin, '"sidewalk" "snow"', OR_CAVEAT_MIN_TOTAL
        )
        assert CAVEAT_TERMS_OR_COMBINED in _codes(sc)

    @pytest.mark.asyncio
    async def test_single_term_no_caveat(self, plugin):
        sc, _ = await _search(plugin, "municipality", 2404)
        assert CAVEAT_TERMS_OR_COMBINED not in _codes(sc)

    @pytest.mark.asyncio
    async def test_single_phrase_no_caveat(self, plugin):
        sc, _ = await _search(plugin, '"right of way"', 500)
        assert CAVEAT_TERMS_OR_COMBINED not in _codes(sc)

    @pytest.mark.parametrize(
        "query",
        [
            '"sidewalk" and "snow" "occupant"',  # lowercase and works live
            '+"sidewalk" +"snow" occupant',
            '"sidewalk" NOT "snow" occupant',
            '"sidewalk" not "snow" occupant',
            '"sidewalk" -"snow" occupant',
            '("notice of claim" OR "claim for damages") AND municipality',
        ],
    )
    @pytest.mark.asyncio
    async def test_any_narrowing_operator_suppresses(self, plugin, query):
        sc, _ = await _search(plugin, query, 900)
        assert CAVEAT_TERMS_OR_COMBINED not in _codes(sc)

    @pytest.mark.parametrize(
        "query,terms,narrowed",
        [
            (NOTICE_QUERY, 6, False),
            ('"information technology" software approval', 3, False),
            ("a OR b c", 3, False),
            ('title:"notice of claim" injury', 2, False),
            ("claim AND notice", 2, True),
        ],
    )
    def test_query_terms(self, query, terms, narrowed):
        got_terms, got_narrowed = EcodePlugin._query_terms(query)
        assert len(got_terms) == terms
        assert got_narrowed is narrowed


# --------------------------------------------------------------------------
# FIX 2 -- informative empty results
# --------------------------------------------------------------------------


class TestNoMatchInCode:
    @pytest.mark.asyncio
    async def test_empty_quoted_phrase(self, plugin):
        sc, text = await _search(plugin, '"tort claim"', 0)
        assert sc["total_count"] == 0
        assert sc["rows"] == []
        # The plain summary is unchanged.
        assert sc["summary"] == "No results for query '\"tort claim\"'."
        assert text.startswith(sc["summary"])
        assert _codes(sc) == [CAVEAT_NO_MATCH_IN_CODE]
        msg = sc["caveats"][0]["message"]
        assert msg in text
        assert "eCode360 library AN6998" in msg
        assert "Municipality of Anchorage" in msg
        assert "state law" in msg and "repealed" in msg
        assert "browse_structure" in msg and "OR between synonyms" in msg

    @pytest.mark.asyncio
    async def test_wording_is_neutral(self, plugin):
        """Say the CODE lacks the text, never that the law does not exist."""
        sc, _ = await _search(plugin, '"purchasing card"', 0)
        msg = sc["caveats"][0]["message"].lower()
        assert "does not appear in the published" in msg
        for claim in ("does not exist", "no such law", "is not the law"):
            assert claim not in msg

    @pytest.mark.parametrize(
        "query,cite",
        [
            ('"Public Employment Relations Act"', '"AS 23.40"'),
            ('"public employment relations act" leave', '"AS 23.40"'),
            ('"AS 39.25"', '"AS 23.40"'),
            ('"Alaska Statutes" tort', '"AS 23.40"'),
        ],
    )
    @pytest.mark.asyncio
    async def test_state_law_hint(self, plugin, query, cite):
        sc, text = await _search(plugin, query, 0)
        assert _codes(sc) == [
            CAVEAT_NO_MATCH_IN_CODE,
            CAVEAT_STATE_LAW_NAMED,
        ]
        hint = sc["caveats"][1]["message"]
        assert hint in text
        assert cite in hint

    @pytest.mark.parametrize(
        "query",
        ['"administrative leave"', '"as 5 parcels"', '"claim for damages"'],
    )
    @pytest.mark.asyncio
    async def test_no_state_law_hint_otherwise(self, plugin, query):
        sc, _ = await _search(plugin, query, 0)
        assert CAVEAT_STATE_LAW_NAMED not in _codes(sc)

    @pytest.mark.asyncio
    async def test_page_past_end_is_not_no_match(self, plugin):
        """618 matches exist; the caller just paged too far. Saying the code
        lacks the text would be false."""
        payload = {
            "results": [], "query": "dog license", "page": "999",
            "maxPages": 31, "totalResultCount": 618,
        }
        sc, _ = await _search(
            plugin, "dog license", 618, page=999, payload=payload
        )
        assert _codes(sc) == [CAVEAT_PAGE_PAST_END]

    @pytest.mark.asyncio
    async def test_unmeasured_total_is_not_no_match(self, plugin):
        """Rows but no totalResultCount is an UNMEASURED total (null), not a
        known zero -- the no-match caveat must not fire on it."""
        payload = {"results": _hits(3), "query": "x", "page": "0"}
        sc, _ = await _search(plugin, "x", 3, payload=payload)
        assert sc["total_count"] is None
        assert CAVEAT_NO_MATCH_IN_CODE not in _codes(sc)
