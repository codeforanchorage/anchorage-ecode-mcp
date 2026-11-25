"""Regressions from the 2026-11-18 bug hunt (385 live calls, hostile
input included). Each case is something the live server or the upstream
actually did."""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from jsonschema import validate

from plugins.ecode.config_schema import EcodePluginConfig
from plugins.ecode.plugin import (
    CAVEAT_SUBSECTION_OF_SECTION,
    SEARCH_OUTPUT_SCHEMA,
    SECTION_OUTPUT_SCHEMA,
    TOC_OUTPUT_SCHEMA,
    EcodePlugin,
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
    p._code_last_updated = AsyncMock(return_value=None)
    return p


def _respond(plugin, handler):
    """handler(path, params) -> httpx.Response kwargs or a json value."""

    async def get(path, params=None, **_):
        out = handler(path, params or {})
        kwargs = out if isinstance(out, dict) and "status_code" in out else {
            "status_code": 200, "json": out}
        return httpx.Response(
            request=httpx.Request("GET", "https://api.ecode360.com" + path), **kwargs)

    plugin.client = MagicMock()
    plugin.client.get = AsyncMock(side_effect=get)


# The body eCode360 sent for '"agreement means"': the matched text is an
# unescaped key inside `hits`, so the response is not JSON.
BROKEN_SEARCH_BODY = '''{
  "results": [ {
      "id": "49247275",
      "number": "3.70.010{18}",
      "title": "\\"Labor agreement\\"",
      "url": "https://ecode360.com/49247275",
      "type": "code",
      "text" : "\\"Labor <em class=\\"highlight\\">agreement\\" means<\\/em> a collective bargaining agreement",
      "pdf": false,
      "hits" : {
          "agreement" means":1                  }
  } ],
  "query": "\\"agreement means\\"", "page": "0", "maxPages": 1, "totalResultCount": 1
}'''


class TestUpstreamInvalidJson:
    @pytest.mark.asyncio
    async def test_search_with_unescaped_hits_is_repaired(self, plugin):
        _respond(plugin, lambda path, params: {
            "status_code": 200, "text": BROKEN_SEARCH_BODY,
            "headers": {"content-type": "application/json"}})
        result = await plugin.execute_tool("search_code", {"query": '"agreement means"'})
        assert result.success, result.error_message
        sc = result.structured_content
        validate(sc, SEARCH_OUTPUT_SCHEMA)
        assert sc["total_count"] == 1
        assert sc["rows"][0]["guid"] == "49247275"
        assert "means a collective bargaining agreement" in sc["rows"][0]["snippet"]

    @pytest.mark.asyncio
    async def test_other_garbage_is_still_a_named_upstream_error(self, plugin):
        _respond(plugin, lambda path, params: {
            "status_code": 200, "text": "<html>oops</html>",
            "headers": {"content-type": "text/html"}})
        result = await plugin.execute_tool("search_code", {"query": "snow"})
        assert not result.success
        assert "non-JSON response" in result.error_message


class TestUpstreamRefusals:
    @pytest.mark.asyncio
    async def test_html_403_is_the_firewall_not_our_credentials(self, plugin):
        _respond(plugin, lambda path, params: {
            "status_code": 403, "text": "<!DOCTYPE html><html>Request blocked</html>"})
        result = await plugin.execute_tool(
            "search_code", {"query": "<script>alert(1)</script>"})
        assert not result.success
        assert "firewall refused this request" in result.error_message
        assert "credentials" not in result.error_message
        assert "ECODE_API_KEY" not in result.error_message

    @pytest.mark.asyncio
    async def test_json_403_is_still_a_credential_problem(self, plugin):
        _respond(plugin, lambda path, params: {
            "status_code": 403, "json": {"message": "Forbidden"}})
        result = await plugin.execute_tool("search_code", {"query": "snow"})
        assert "refusing this server's credentials" in result.error_message

    @pytest.mark.asyncio
    async def test_node_above_chapter_level_is_explained_without_retries(self, plugin):
        _respond(plugin, lambda path, params: {
            "status_code": 500, "json": {"message": (
                "You have selected a level above the chapter level.  Content "
                "requests must be at the chapter level or below.")}})
        result = await plugin.execute_tool("get_section", {"guid": "ROOT"})
        assert not result.success
        assert "above the chapter level" in result.error_message
        assert "browse_structure" in result.error_message
        assert "attempt" not in result.error_message
        assert plugin.client.get.call_count == 1


class TestArgumentShapes:
    @pytest.mark.parametrize("query", [5, ["snow"], {"q": "snow"}, True])
    @pytest.mark.asyncio
    async def test_non_string_query_is_a_sentence(self, plugin, query):
        _respond(plugin, lambda path, params: {})
        result = await plugin.execute_tool("search_code", {"query": query})
        assert not result.success
        assert "query must be a string" in result.error_message
        plugin.client.get.assert_not_called()

    @pytest.mark.parametrize("arguments", [[], "x", 5])
    @pytest.mark.asyncio
    async def test_non_object_arguments(self, plugin, arguments):
        _respond(plugin, lambda path, params: {})
        for tool in ("search_code", "get_section", "find_references"):
            result = await plugin.execute_tool(tool, arguments)
            assert not result.success
            assert "must be an object" in result.error_message
            assert "attribute" not in result.error_message

    @pytest.mark.asyncio
    async def test_missing_arguments_object_is_empty_arguments(self, plugin):
        _respond(plugin, lambda path, params: {"results": {"name": "Anchorage"}})
        result = await plugin.execute_tool("get_customer_info", None)
        assert result.success


SECTION = {"id": "49350138", "number": "§ 9.28.019",
           "title": "Valid operator's license required.", "type": "section",
           "content": "Valid operator's license required. (AO No. 2001-72)",
           "attachments": {}, "images": [],
           "children": [{"id": "1", "number": "B.", "content": "It is unlawful.",
                         "title": "Valid operator's license required.",
                         "children": [], "type": "subsection"}]}


class TestSubsectionReference:
    @pytest.mark.parametrize("asked, sub", [("9.28.019B.2", "B.2"), ("§ 9.28.019B", "B")])
    @pytest.mark.asyncio
    async def test_subsection_number_reads_its_section(self, plugin, asked, sub):
        """A fine schedule cites '§ 9.28.019B.2'; get_section refused it."""

        def upstream(path, params):
            if "/search" in path:
                hits = ([{"id": "49350138", "number": "§ 9.28.019", "title": "T",
                          "type": "code"}] if params["query"] == "9.28.019" else [])
                return {"results": hits, "totalResultCount": len(hits), "maxPages": 1}
            return SECTION

        _respond(plugin, upstream)
        result = await plugin.execute_tool("get_section", {"guid": asked})
        assert result.success, result.error_message
        sc = result.structured_content
        validate(sc, SECTION_OUTPUT_SCHEMA)
        assert sc["guid"] == "49350138"
        note = next(c["message"] for c in sc["caveats"]
                    if c["code"] == CAVEAT_SUBSECTION_OF_SECTION)
        assert f"Subsection {sub} was asked for" in note
        assert note in result.content[0]["text"]
        # the shared cached node was not written to
        assert "_subsection" not in SECTION

    @pytest.mark.asyncio
    async def test_unknown_section_is_still_refused(self, plugin):
        _respond(plugin, lambda path, params: {
            "results": [], "totalResultCount": 0, "maxPages": 0})
        result = await plugin.execute_tool("get_section", {"guid": "99.99.999B"})
        assert not result.success
        assert "No section numbered '99.99.999'" in result.error_message


class TestMarkupInSnippets:
    @pytest.mark.asyncio
    async def test_entities_are_decoded(self, plugin):
        _respond(plugin, lambda path, params: {
            "results": [{"id": "7", "number": "", "pdf": True, "type": "doc",
                         "title": "Old Title 21 &amp; tables",
                         "url": "https://live.ecode360.com/attachment/1/x.pdf",
                         "text": "21.05.010&nbsp;Adoption; <em>scope</em> &lt;of&gt; chapter"}],
            "totalResultCount": 1, "maxPages": 1})
        result = await plugin.execute_tool("find_references", {"section": "21.05.010"})
        row = result.structured_content["references"][0]
        assert row["snippet"] == "21.05.010 Adoption; scope <of> chapter"
        assert row["title"] == "Old Title 21 & tables"
        assert "&nbsp;" not in result.content[0]["text"]


class TestOutlineOddNodes:
    @pytest.mark.asyncio
    async def test_children_without_a_guid_do_not_shift_the_outline(self, plugin):
        toc = {"guid": "AN6998", "title": "Code", "children": [
            {"label": "Part", "number": "I", "title": "No guid here", "children": []},
            None,
            {"guid": "2", "label": "Part", "number": "II", "title": "CODE", "children": [
                {"guid": "3", "label": "Title", "number": "1", "title": "General",
                 "children": None}]},
        ]}
        _respond(plugin, lambda path, params: toc)
        result = await plugin.execute_tool("get_table_of_contents", {})
        assert result.success, result.error_message
        sc = result.structured_content
        validate(sc, TOC_OUTPUT_SCHEMA)
        assert [c["guid"] for c in sc["children"]] == ["2"]
        assert sc["children"][0]["title"] == "CODE"
        assert sc["children"][0]["children"][0]["guid"] == "3"

    @pytest.mark.asyncio
    async def test_browse_structure_survives_a_null_child(self, plugin):
        _respond(plugin, lambda path, params: {"error": False, "results": {
            "guid": "2", "title": "CODE", "children": [
                None, {"guid": "3", "label": "Title", "number": "1", "title": "General"}]}})
        result = await plugin.execute_tool("browse_structure", {"guid": "2"})
        assert result.success, result.error_message
        assert [c["guid"] for c in result.structured_content["children"]] == ["3"]


class TestBrowseStructureByReference:
    @pytest.mark.asyncio
    async def test_section_number_is_resolved(self, plugin):
        """browse_structure('R3.70.120') used to be refused with 'that
        looks like a section NUMBER'."""

        def upstream(path, params):
            if "/search" in path:
                return {"results": [{"id": "48977680", "number": "§ R3.70.120",
                                     "title": "Petitions", "type": "code"}],
                        "totalResultCount": 1, "maxPages": 1}
            assert path.endswith("/structure/48977680"), path
            return {"error": False, "results": {
                "guid": "48977680", "number": "§ R3.70.120", "title": "Petitions",
                "children": [{"guid": "1", "label": "Litem", "number": "A."}]}}

        _respond(plugin, upstream)
        result = await plugin.execute_tool("browse_structure", {"guid": "R3.70.120"})
        assert result.success, result.error_message
        assert result.structured_content["node"]["guid"] == "48977680"

    @pytest.mark.asyncio
    async def test_guid_and_default_still_work(self, plugin):
        seen = []

        def upstream(path, params):
            seen.append(path)
            return {"error": False, "results": {"guid": "ROOT", "children": []}}

        _respond(plugin, upstream)
        assert (await plugin.execute_tool("browse_structure", {})).success
        assert (await plugin.execute_tool("browse_structure", {"guid": "49245794"})).success
        assert seen[0].endswith("/structure/ROOT")
        assert seen[1].endswith("/structure/49245794")
