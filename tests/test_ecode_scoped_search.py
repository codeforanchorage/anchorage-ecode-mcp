"""search_code(within=...) -- search limited to a title, chapter or section.

From the 2026-11-17 prod-log review: models wrote title:9 to mean "in
Title 9". eCode360 has no such filter (verified live: every scoping and
page-size parameter is ignored), so the server reads up to
SCOPE_SCAN_PAGES upstream pages and keeps the hits that sit under the
requested node of the cached table of contents.

The upstream is routed by (path, query, page), never by call order: the
pages of one scan are fetched concurrently.
"""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from jsonschema import validate

from plugins.ecode.config_schema import EcodePluginConfig
from plugins.ecode.plugin import (
    CAVEAT_NO_MATCH_IN_CODE,
    CAVEAT_NO_MATCH_IN_SCOPE,
    CAVEAT_PAGE_PAST_END,
    CAVEAT_SCOPE_SCAN_PARTIAL,
    CAVEAT_SCOPE_UNPLACED_HITS,
    CAVEAT_TITLE_FIELD_IS_HEADING,
    SCOPE_SCAN_PAGES,
    SEARCH_OUTPUT_SCHEMA,
    EcodePlugin,
)

REQ = httpx.Request("GET", "https://api.ecode360.com/v1/x")

CONFIG = {
    "enabled": True,
    "base_url": "https://api.ecode360.com",
    "customer_id": "AN6998",
    "city_name": "Municipality of Anchorage",
    "timeout": 20,
}


def _json(body, status=200):
    return httpx.Response(status_code=status, json=body, request=REQ)


def _node(guid, label, number, title, type_="section", index=None, kids=()):
    return {
        "guid": guid,
        "label": label,
        "number": number,
        "indexNum": index if index is not None else number,
        "title": title,
        "type": type_,
        "children": list(kids),
    }


# The live outline's shape, cut down. The Charter's "Section 17.05" and the
# code's "Chapter 17.05" share a number, as they do in the real code.
TOC = {
    "guid": "AN6998",
    "type": "code",
    "label": "",
    "number": "",
    "title": "Municipality of Anchorage, AK",
    "children": [
        _node("cha", "Title", "CHA", "Charter", "chapter", "Ttl CHA", [
            _node("c1705", "Section", "Section\xa017.05", "Public meetings.",
                  "section", "§\xa017.05"),
        ]),
        _node("t9", "Title", "9", "Vehicles and Traffic", "chapter",
              "Ttl 9", [
            _node("ch904", "Chapter", "9.04", "DEFINITIONS", "article",
                  "Ch 9.04", [
                _node("s904010", "Section", "§\xa09.04.010", "Definitions."),
            ]),
            _node("ch928", "Chapter", "9.28", "SERIOUS TRAFFIC OFFENSES",
                  "article", "Ch 9.28", [
                _node("s928020", "Section", "§\xa09.28.020", "DUI."),
            ]),
        ]),
        _node("t17", "Title", "17", "Animals", "chapter", "Ttl 17", [
            _node("ch1705", "Chapter", "17.05", "GENERAL PROVISIONS",
                  "article", "Ch 17.05", [
                _node("s1705010", "Section", "§\xa017.05.010",
                      "Definitions."),
            ]),
        ]),
        _node("t21", "Title", "21", "Land Use Planning", "chapter",
              "Ttl 21", [
            _node("ch2115", "Chapter", "21.15", "RULES", "article",
                  "Ch 21.15", [
                _node("s2115040", "Section", "§\xa021.15.040",
                      "Definitions."),
            ]),
        ]),
    ],
}


def _hit(guid, number, title="T", **extra):
    return {"id": guid, "number": number, "title": title,
            "text": "snippet", "type": "code", **extra}


DUI = _hit("s928020", "§\xa09.28.020", "DUI.")
DEFS_9 = _hit("s904010", "§\xa09.04.010", "Definitions.")
# Definition nodes are NOT in the outline; their number extends a section's.
DEF_VEHICLE = _hit("d1", "9.04.010B{116}", '"Vehicle"')
DEF_LOT = _hit("d2", "21.15.040{62}", '"Lot"')
CH_928 = _hit("ch928", "Ch 9.28", "SERIOUS TRAFFIC OFFENSES")
ZONING = _hit("s2115040", "§\xa021.15.040", "Definitions.")
PDF = _hit("753304506", None, "Anchorage OLD Title 21", pdf=True,
           type="doc",
           url="https://live.ecode360.com/documents/AN6998/public/x.pdf")
ORPHAN = _hit("zz9", "Appendix Q", "Not in the outline")

MIXED = [DEF_VEHICLE, ZONING, DUI, PDF, DEF_LOT, CH_928, DEFS_9]


def _plugin(pages, total=None, max_pages=None, toc=TOC, failing=()):
    """`pages` maps upstream page number -> list of hits for any query."""
    calls = []
    if total is None:
        total = sum(len(v) for v in pages.values())
    if max_pages is None:
        max_pages = max(pages, default=0) + 1

    async def fake_get(path, params=None, **_kw):
        calls.append((path, dict(params or {})))
        if path.endswith("/code/toc"):
            return _json(toc)
        if path.endswith("/search"):
            page = int(params["page"])
            if page in failing:
                return _json({"message": "boom"}, 500)
            return _json({
                "results": pages.get(page, []),
                "query": params["query"],
                "page": str(page),
                "maxPages": max_pages,
                "totalResultCount": total,
            })
        return _json({"message": "unexpected"}, 404)

    p = EcodePlugin(CONFIG)
    p.plugin_config = EcodePluginConfig(**CONFIG)
    p.client = MagicMock()
    p.client.get = AsyncMock(side_effect=fake_get)
    p.calls = calls
    return p


def _searched(plugin):
    return sorted(
        params["page"] for path, params in plugin.calls
        if path.endswith("/search")
    )


async def _run(plugin, within, query='"vehicle"', page=0):
    result = await plugin.execute_tool(
        "search_code", {"query": query, "within": within, "page": page}
    )
    assert result.success, result.error_message
    sc = result.structured_content
    validate(sc, SEARCH_OUTPUT_SCHEMA)
    text = result.content[0]["text"]
    # One list feeds both halves: every caveat is in the prose too.
    for caveat in sc["caveats"]:
        assert caveat["message"] in text
    return sc, text


def _codes(sc):
    return [c["code"] for c in sc["caveats"]]


def _guids(sc):
    return [row["guid"] for row in sc["rows"]]


class TestScopeFiltering:
    @pytest.mark.asyncio
    async def test_title_keeps_its_hits_in_upstream_order(self):
        p = _plugin({0: MIXED})
        sc, text = await _run(p, "Title 9")

        # Outline nodes by guid; the definition by the section it extends.
        assert _guids(sc) == ["d1", "s928020", "ch928", "s904010"]
        assert sc["total_count"] == 4
        assert sc["within"] == {
            "guid": "t9",
            "display": "Title 9 Vehicles and Traffic",
            "hits_checked": 7,
            "query_total": 7,
        }
        assert "within Title 9 Vehicles and Traffic" in sc["summary"]
        assert "all 7 hit(s) for the query were checked" in sc["summary"]
        assert "1. 9.04.010B{116}" in text

    @pytest.mark.asyncio
    async def test_the_query_goes_upstream_unmodified(self):
        p = _plugin({0: MIXED})
        await _run(p, "Title 9", query='"a b" AND c')
        queries = {
            params["query"] for path, params in p.calls
            if path.endswith("/search")
        }
        assert queries == {'"a b" AND c'}

    @pytest.mark.asyncio
    async def test_chapter_scope(self):
        sc, _ = await _run(_plugin({0: MIXED}), "Chapter 9.28")
        assert _guids(sc) == ["s928020", "ch928"]

    @pytest.mark.asyncio
    async def test_section_scope_keeps_its_definitions(self):
        sc, _ = await _run(_plugin({0: MIXED}), "9.04.010")
        assert _guids(sc) == ["d1", "s904010"]

    @pytest.mark.parametrize("within", ["t9", "9", " Title 9 ", "ttl 9"])
    @pytest.mark.asyncio
    async def test_guid_bare_number_and_spelling_variants(self, within):
        sc, _ = await _run(_plugin({0: MIXED}), within)
        assert sc["within"]["guid"] == "t9"
        assert len(sc["rows"]) == 4

    @pytest.mark.asyncio
    async def test_definition_of_another_title_is_left_out(self):
        sc, _ = await _run(_plugin({0: [DEF_LOT, DEF_VEHICLE]}), "Title 21")
        assert _guids(sc) == ["d2"]

    @pytest.mark.asyncio
    async def test_charter_section_scope(self):
        hit = _hit("c1705", "Section\xa017.05", "Public meetings.")
        other = _hit("s1705010", "§\xa017.05.010", "Definitions.")
        sc, _ = await _run(
            _plugin({0: [other, hit]}), "Charter Section 17.05"
        )
        assert _guids(sc) == ["c1705"]

    @pytest.mark.asyncio
    async def test_blank_within_is_an_ordinary_search(self):
        p = _plugin({0: MIXED})
        result = await p.execute_tool(
            "search_code", {"query": "vehicle", "within": "  "}
        )
        sc = result.structured_content
        validate(sc, SEARCH_OUTPUT_SCHEMA)
        assert "within" not in sc
        assert len(sc["rows"]) == len(MIXED)
        assert all(not path.endswith("/code/toc") for path, _ in p.calls)


class TestUnplacedHits:
    @pytest.mark.asyncio
    async def test_pdf_documents_are_left_out_and_reported(self):
        sc, _ = await _run(_plugin({0: MIXED}), "Title 21")
        assert "753304506" not in _guids(sc)
        assert _codes(sc) == [CAVEAT_SCOPE_UNPLACED_HITS]
        assert "1 PDF document hit(s)" in sc["caveats"][0]["message"]

    @pytest.mark.asyncio
    async def test_a_node_unknown_to_the_outline_is_not_guessed(self):
        sc, _ = await _run(_plugin({0: [ORPHAN, DUI]}), "Title 9")
        assert _guids(sc) == ["s928020"]
        message = sc["caveats"][0]["message"]
        assert "1 hit(s) with no place in the outline" in message
        assert "PDF" not in message

    @pytest.mark.asyncio
    async def test_no_caveat_when_every_hit_is_placed(self):
        sc, _ = await _run(_plugin({0: [DUI, ZONING]}), "Title 9")
        assert _codes(sc) == []


class TestNothingInScope:
    @pytest.mark.asyncio
    async def test_matches_elsewhere_only(self):
        sc, text = await _run(_plugin({0: [ZONING, DEF_LOT]}), "Title 9")
        assert sc["rows"] == []
        # Every hit was checked and none is in scope: a complete zero.
        assert sc["total_count"] == 0
        assert _codes(sc) == [CAVEAT_NO_MATCH_IN_SCOPE]
        assert "search again without `within`" in text
        assert CAVEAT_NO_MATCH_IN_CODE not in _codes(sc)

    @pytest.mark.asyncio
    async def test_query_matches_nothing_anywhere(self):
        sc, _ = await _run(_plugin({}, total=0, max_pages=0), "Title 9")
        assert sc["total_count"] == 0
        assert _codes(sc) == [CAVEAT_NO_MATCH_IN_CODE]
        assert sc["within"]["query_total"] == 0


def _many_pages(n_pages, in_scope_every=4):
    """n_pages full upstream pages; every Nth hit is in Title 9."""
    pages = {}
    for page in range(n_pages):
        hits = []
        for i in range(20):
            n = page * 20 + i
            hits.append(DUI if n % in_scope_every == 0 else ZONING)
        pages[page] = hits
    return pages


class TestPartialScan:
    @pytest.mark.asyncio
    async def test_first_window_reads_ten_pages_and_says_more_exist(self):
        p = _plugin(_many_pages(25), total=500, max_pages=25)
        sc, text = await _run(p, "Title 9")

        assert _searched(p) == list(range(SCOPE_SCAN_PAGES))
        assert len(sc["rows"]) == 50
        # 300 hits were never looked at: the in-scope count is unknown.
        assert sc["total_count"] is None
        assert sc["within"]["hits_checked"] == 200
        assert sc["within"]["query_total"] == 500
        assert CAVEAT_SCOPE_SCAN_PARTIAL in _codes(sc)
        assert "hits 1-200 of 500" in sc["summary"]
        assert "page=1" in text

    @pytest.mark.asyncio
    async def test_page_one_reads_the_next_window(self):
        p = _plugin(_many_pages(25), total=500, max_pages=25)
        sc, _ = await _run(p, "Title 9", page=1)
        assert _searched(p) == list(range(10, 20))
        assert "hits 201-400 of 500" in sc["summary"]
        assert CAVEAT_SCOPE_SCAN_PARTIAL in _codes(sc)

    @pytest.mark.asyncio
    async def test_last_window_stops_at_the_last_page(self):
        p = _plugin(_many_pages(25), total=500, max_pages=25)
        sc, _ = await _run(p, "Title 9", page=2)
        assert _searched(p) == [20, 21, 22, 23, 24]
        assert CAVEAT_SCOPE_SCAN_PARTIAL not in _codes(sc)
        # Still one window of several, so still not a complete count.
        assert sc["total_count"] is None
        assert "hits 401-500 of 500" in sc["summary"]

    @pytest.mark.asyncio
    async def test_exactly_ten_pages_is_a_complete_scan(self):
        p = _plugin(_many_pages(10), total=200, max_pages=10)
        sc, _ = await _run(p, "Title 9")
        assert sc["total_count"] == 50
        assert CAVEAT_SCOPE_SCAN_PARTIAL not in _codes(sc)

    @pytest.mark.asyncio
    async def test_nothing_in_scope_so_far_is_not_a_complete_zero(self):
        pages = {n: [ZONING] * 20 for n in range(25)}
        sc, _ = await _run(
            _plugin(pages, total=500, max_pages=25), "Title 9"
        )
        assert sc["rows"] == []
        assert sc["total_count"] is None
        assert _codes(sc) == [
            CAVEAT_NO_MATCH_IN_SCOPE,
            CAVEAT_SCOPE_SCAN_PARTIAL,
        ]
        assert "among the hits checked so far" in sc["caveats"][0]["message"]

    @pytest.mark.asyncio
    async def test_page_past_the_end(self):
        p = _plugin(_many_pages(25), total=500, max_pages=25)
        sc, _ = await _run(p, "Title 9", page=7)
        assert sc["rows"] == []
        assert sc["total_count"] is None
        assert _codes(sc) == [CAVEAT_PAGE_PAST_END]
        assert "between 0 and 2" in sc["caveats"][0]["message"]

    @pytest.mark.asyncio
    async def test_a_failed_page_fails_the_call_rather_than_hiding_hits(
        self, monkeypatch
    ):
        import plugins.ecode.plugin as ecode_mod

        monkeypatch.setattr(ecode_mod, "RETRY_BACKOFF_S", 0.0)
        p = _plugin(_many_pages(5), total=100, max_pages=5, failing={3})
        result = await p.execute_tool(
            "search_code", {"query": "vehicle", "within": "Title 9"}
        )
        assert result.success is False


class TestScopeErrors:
    @pytest.mark.parametrize(
        "within,expect",
        [
            ("17.05", "matches 2 different entries"),
            ("Title 99", "does not name a title, chapter or section"),
            ("ROOT", "is not a title, chapter, section or guid"),
            ("AN6998", "names the whole code"),
            ("d1", "is not in the outline"),
            ("traffic law", "is not a title, chapter, section or guid"),
        ],
    )
    @pytest.mark.asyncio
    async def test_bad_scope_is_an_error_and_never_searches(
        self, within, expect
    ):
        p = _plugin({0: MIXED})
        result = await p.execute_tool(
            "search_code", {"query": "vehicle", "within": within}
        )
        assert result.success is False
        assert expect in result.error_message
        assert _searched(p) == []

    @pytest.mark.asyncio
    async def test_ambiguous_scope_names_both_candidates(self):
        p = _plugin({0: MIXED})
        result = await p.execute_tool(
            "search_code", {"query": "vehicle", "within": "17.05"}
        )
        assert "guid c1705" in result.error_message
        assert "guid ch1705" in result.error_message

    @pytest.mark.asyncio
    async def test_outline_unavailable_is_said_plainly(self):
        fallback = dict(TOC, _fallback="structure/ROOT")
        p = _plugin({0: MIXED}, toc=fallback)
        result = await p.execute_tool(
            "search_code", {"query": "vehicle", "within": "Title 9"}
        )
        assert result.success is False
        assert "outline of the code could not be loaded" in (
            result.error_message
        )


class TestGuidancePointsAtWithin:
    @pytest.mark.asyncio
    async def test_title_number_caveat_recommends_within(self):
        p = _plugin({0: []}, total=0, max_pages=0)
        result = await p.execute_tool(
            "search_code", {"query": '"tow truck" AND title:9'}
        )
        sc = result.structured_content
        message = next(
            c["message"] for c in sc["caveats"]
            if c["code"] == CAVEAT_TITLE_FIELD_IS_HEADING
        )
        assert "within='Title 9'" in message

    def test_tool_schema_declares_within(self):
        p = EcodePlugin(CONFIG)
        p.plugin_config = EcodePluginConfig(**CONFIG)
        tool = next(t for t in p.get_tools() if t.name == "search_code")
        assert "within" in tool.input_schema["properties"]
        assert tool.input_schema["required"] == ["query"]
        assert "within" in tool.description
