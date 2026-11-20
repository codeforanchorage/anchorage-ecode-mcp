"""A section number is resolved from the table of contents, not by search
(1.17.0).

Prod, 2026-11-20: reads by section number took 4 s or more in 13 of 30
calls, reads by guid in 0 of 9. The difference was one search per number,
and eCode360's search stalls for about 5 s one time in eight. The outline
is already in memory and listed 92 of the 93 numbers asked for that day;
the 93rd does not exist. Search remains the fallback.
"""

import copy

import httpx
import pytest
from unittest.mock import AsyncMock, MagicMock

from plugins.ecode.config_schema import EcodePluginConfig
from plugins.ecode.plugin import (
    CAVEAT_RESOLVED_FROM_SECTION_NUMBER,
    EcodePlugin,
)
from tests.test_ecode_scoped_search import CONFIG, TOC, _node

pytestmark = pytest.mark.outline_sections

REQ = httpx.Request("GET", "https://api.ecode360.com/v1/x")


def _json(body, status=200):
    return httpx.Response(status_code=status, json=body, request=REQ)


def _plugin(toc=TOC, toc_status=200, search_hits=(), gone=()):
    """`search_hits`: (guid, number) pairs any search returns.
    `gone`: guids whose content is a 404."""
    calls = []

    async def fake_get(path, params=None, **_kw):
        calls.append(path if not params else f"{path}?{params.get('query')}")
        if path.endswith("/code/toc"):
            return _json(toc, toc_status)
        if path.endswith("/search"):
            return _json({
                "results": [{"id": g, "number": n, "title": "T",
                             "text": "x", "type": "code", "pdf": False}
                            for g, n in search_hits],
                "page": "0", "maxPages": 1,
                "totalResultCount": len(search_hits)})
        guid = path.rsplit("/", 1)[-1]
        if "/code/content/" in path:
            if guid in gone:
                return _json({"message": "Could not find Content"}, 404)
            return _json({"id": guid, "number": "", "title": f"node {guid}",
                          "content": "Text.", "children": [],
                          "attachments": {}, "images": []})
        if "/structure/" in path:
            return _json({"error": False, "results": {
                "guid": guid, "title": "n", "number": "", "label": "Section",
                "children": []}})
        return _json({"message": "unexpected"}, 404)

    p = EcodePlugin(CONFIG)
    p.plugin_config = EcodePluginConfig(**CONFIG)
    p.client = MagicMock()
    p.client.get = AsyncMock(side_effect=fake_get)
    p.calls = calls
    return p


def _searches(p):
    return [c for c in p.calls if "/search" in c]


def _contents(p):
    return [c.rsplit("/", 1)[-1] for c in p.calls if "/code/content/" in c]


class TestFromTheOutline:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("ref", ["9.28.020", "§ 9.28.020", " 9.28.020 "])
    async def test_get_section_makes_no_search(self, ref):
        p = _plugin()
        result = await p.execute_tool("get_section", {"guid": ref})

        assert result.success, result.error_message
        assert _searches(p) == []
        assert _contents(p) == ["s928020"]
        codes = [c["code"] for c in result.structured_content["caveats"]]
        assert CAVEAT_RESOLVED_FROM_SECTION_NUMBER in codes

    @pytest.mark.asyncio
    async def test_a_batch_makes_no_search_and_reads_the_outline_once(self):
        p = _plugin()
        result = await p.execute_tool(
            "get_sections",
            {"sections": ["9.28.020", "9.04.010", "21.15.040", "17.05.010"]},
        )

        assert result.success, result.error_message
        assert _searches(p) == []
        assert sum(1 for c in p.calls if c.endswith("/code/toc")) == 1
        assert sorted(_contents(p)) == [
            "s1705010", "s2115040", "s904010", "s928020",
        ]

    @pytest.mark.asyncio
    async def test_browse_structure_too(self):
        p = _plugin()
        result = await p.execute_tool("browse_structure", {"guid": "9.28.020"})

        assert result.success, result.error_message
        assert _searches(p) == []
        assert p.calls[-1].endswith("/structure/s928020")

    @pytest.mark.asyncio
    async def test_subsection_reads_its_section(self):
        """'9.28.020B.2' is not in the outline or in search; the section
        it belongs to is, and comes from the outline."""
        p = _plugin()
        result = await p.execute_tool("get_section", {"guid": "9.28.020B.2"})

        assert result.success, result.error_message
        assert _contents(p) == ["s928020"]
        # One search: for the subsection form, which nothing lists.
        assert len(_searches(p)) == 1

    @pytest.mark.asyncio
    async def test_a_two_part_number_is_still_ambiguous(self):
        p = _plugin()
        result = await p.execute_tool("get_section", {"guid": "17.05"})

        assert result.success is False
        assert "matches 2 different entries" in result.error_message


class TestSearchIsStillTheFallback:
    @pytest.mark.asyncio
    async def test_number_the_outline_does_not_list(self):
        p = _plugin(search_hits=[("found1", "§ 9.99.999")])
        result = await p.execute_tool("get_section", {"guid": "9.99.999"})

        assert result.success, result.error_message
        assert len(_searches(p)) == 1
        assert _contents(p) == ["found1"]

    @pytest.mark.asyncio
    async def test_number_that_exists_nowhere(self):
        p = _plugin()
        result = await p.execute_tool("get_section", {"guid": "9.99.999"})

        assert result.success is False
        assert "No section numbered '9.99.999'" in result.error_message

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [500, 404])
    async def test_outline_unavailable(self, status):
        p = _plugin(toc_status=status, search_hits=[("s928020", "§ 9.28.020")])
        result = await p.execute_tool("get_section", {"guid": "9.28.020"})

        assert result.success, result.error_message
        assert _contents(p) == ["s928020"]
        assert len(_searches(p)) == 1

    @pytest.mark.asyncio
    async def test_building_code_part_is_not_taken_for_a_section(self):
        """The outline labels parts of the building codes 'Section' too;
        only a node of type section is one."""
        toc = copy.deepcopy(TOC)
        toc["children"].append(
            _node("t23", "Title", "23", "Building Codes", "chapter", "Ttl 23", [
                _node("part1", "Section", "§\xa023.10.101", "General.",
                      "article"),
            ])
        )
        p = _plugin(toc=toc, search_hits=[("real", "§ 23.10.101")])
        result = await p.execute_tool("get_section", {"guid": "23.10.101"})

        assert result.success, result.error_message
        assert _contents(p) == ["real"]

    @pytest.mark.asyncio
    async def test_number_listed_twice_is_left_to_search(self):
        toc = copy.deepcopy(TOC)
        toc["children"][1]["children"][1]["children"].append(
            _node("dup", "Section", "§\xa09.28.020", "DUI (old).")
        )
        p = _plugin(toc=toc, search_hits=[("s928020", "§ 9.28.020")])
        result = await p.execute_tool("get_section", {"guid": "9.28.020"})

        assert result.success, result.error_message
        assert len(_searches(p)) == 1
        assert _contents(p) == ["s928020"]


class TestStaleOutline:
    """The outline is kept for an hour; a republish in that time can
    retire a guid taken from it."""

    @pytest.mark.asyncio
    async def test_search_is_asked_before_giving_up(self):
        p = _plugin(gone={"s928020"}, search_hits=[("new928", "§ 9.28.020")])
        result = await p.execute_tool("get_section", {"guid": "9.28.020"})

        assert result.success, result.error_message
        assert _contents(p) == ["s928020", "new928"]

    @pytest.mark.asyncio
    async def test_when_search_agrees_the_section_really_is_gone(self):
        p = _plugin(gone={"s928020"}, search_hits=[("s928020", "§ 9.28.020")])
        result = await p.execute_tool("get_section", {"guid": "9.28.020"})

        assert result.success is False
        assert "no such entry" in result.error_message
        assert _contents(p) == ["s928020"]

    @pytest.mark.asyncio
    async def test_a_guid_the_caller_gave_is_not_second_guessed(self):
        p = _plugin(gone={"s928020"}, search_hits=[("new928", "§ 9.28.020")])
        result = await p.execute_tool("get_section", {"guid": "s928020"})

        assert result.success is False
        assert _searches(p) == []


class TestTrailingPunctuation:
    """'21.05.010.' -- copied from the end of a sentence -- was taken for
    a guid and refused as 'not alphanumeric'."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "ref", ["9.28.020.", "9.28.020,", "§ 9.28.020;", "Section 9.28.020.", "9.28.020. "]
    )
    async def test_section_number(self, ref):
        p = _plugin()
        result = await p.execute_tool("get_section", {"guid": ref})

        assert result.success, result.error_message
        assert _contents(p) == ["s928020"]

    @pytest.mark.asyncio
    async def test_chapter_and_scope(self):
        p = _plugin()
        assert (await p.execute_tool("browse_structure", {"guid": "Chapter 9.28."})).success
        assert p.calls[-1].endswith("/structure/ch928")

    def test_a_guid_is_untouched(self):
        assert EcodePlugin._parse_ref("49401992") is None
        assert EcodePlugin._parse_ref("49401992.") is None
