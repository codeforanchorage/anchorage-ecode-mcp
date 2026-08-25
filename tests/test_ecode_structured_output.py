"""Structured output must actually conform to the schemas it advertises.

A declared outputSchema is BINDING: the MCP spec says servers MUST return
conforming structured results and clients SHOULD validate them. So these
tests run real tool output through jsonschema rather than eyeballing the
shape, and they concentrate on the awkward branches -- no matches, the last
page, a page past the end, a guid that resolves to nothing, a section whose
body is an attachment, a node with no children -- because the happy path is
the one place this cannot go wrong.
"""

import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from jsonschema import validate

from plugins.ecode.config_schema import EcodePluginConfig
from plugins.ecode.plugin import (
    CAVEAT_BODY_IS_ATTACHMENT,
    CAVEAT_MORE_PAGES,
    CAVEAT_NODE_HAS_NO_CHILDREN,
    CAVEAT_PAGE_PAST_END,
    CAVEAT_RENDERED_TEXT_TRUNCATED,
    CAVEAT_SECTION_NOT_IN_FORCE,
    CAVEAT_TOC_ENDPOINT_UNAVAILABLE,
    MAX_CONTENT_CHARS,
    SEARCH_OUTPUT_SCHEMA,
    SECTION_OUTPUT_SCHEMA,
    STRUCTURE_OUTPUT_SCHEMA,
    TOC_OUTPUT_SCHEMA,
    EcodePlugin,
)


@pytest.fixture
def ecode_config():
    return {
        "enabled": True,
        "base_url": "https://api.ecode360.com",
        "customer_id": "AN6998",
        "city_name": "Municipality of Anchorage",
        "timeout": 20,
    }


@pytest.fixture
def plugin(ecode_config):
    p = EcodePlugin(ecode_config)
    p.plugin_config = EcodePluginConfig(**ecode_config)
    return p


def _respond(payload):
    return httpx.Response(
        status_code=200,
        json=payload,
        request=httpx.Request("GET", "https://api.ecode360.com/v1/x"),
    )


def _wire(plugin, payload):
    plugin.client = MagicMock()
    plugin.client.get = AsyncMock(return_value=_respond(payload))


def _structured(result):
    assert result.success, result.error_message
    assert result.structured_content is not None, (
        "tool declares an outputSchema but returned no structuredContent"
    )
    return result.structured_content


def _text(result):
    return result.content[0]["text"]


# --------------------------------------------------------------------------
# C1 -- titles and annotations
# --------------------------------------------------------------------------


class TestToolMetadata:
    def test_every_tool_has_a_title(self, plugin):
        """A tool added without a TOOL_TITLES entry would silently fall back
        to the prefixed wire name, so fail loudly instead."""
        tools = plugin.get_tools()
        assert tools
        for t in tools:
            assert t.title, f"{t.name} has no title (add it to TOOL_TITLES)"

    def test_no_stale_title_entries(self, plugin):
        """A renamed or removed tool must not leave a key behind."""
        names = {t.name for t in plugin.get_tools()}
        stale = set(EcodePlugin.TOOL_TITLES) - names
        assert not stale, f"TOOL_TITLES has entries for missing tools: {stale}"

    def test_tools_are_read_only_without_idempotent_hint(self, plugin):
        """idempotentHint is documented as meaningful only when readOnlyHint
        is false, so advertising it on these tools would be noise."""
        for t in plugin.get_tools():
            assert t.annotations is not None, t.name
            assert t.annotations.get("readOnlyHint") is True, t.name
            assert t.annotations.get("openWorldHint") is True, t.name
            assert "idempotentHint" not in t.annotations, t.name

    def test_customer_info_declares_no_output_schema(self, plugin):
        """Deliberately skipped: get_customer_info is metadata prose whose
        value IS the prose. A schema there is ceremony with no consumer."""
        by_name = {t.name: t for t in plugin.get_tools()}
        assert by_name["get_customer_info"].output_schema is None

    def test_the_four_converted_tools_declare_a_schema(self, plugin):
        by_name = {t.name: t for t in plugin.get_tools()}
        for name in (
            "search_code",
            "get_section",
            "browse_structure",
            "get_table_of_contents",
        ):
            assert by_name[name].output_schema is not None, name


# --------------------------------------------------------------------------
# C7 -- the zero-result trap
# --------------------------------------------------------------------------


class TestZeroResultPathsStillEmitStructuredContent:
    """Any branch that short-circuits BEFORE the shared formatter would
    advertise an outputSchema and then return nothing. Invisible to
    happy-path tests, and a search matching nothing is common here."""

    @pytest.mark.asyncio
    async def test_search_with_no_matches(self, plugin):
        _wire(plugin, {"results": [], "query": "zzzznotaword", "page": "0"})
        result = await plugin.execute_tool(
            "search_code", {"query": "zzzznotaword"}
        )
        structured = _structured(result)
        validate(structured, SEARCH_OUTPUT_SCHEMA)
        assert structured["rows"] == []

    @pytest.mark.asyncio
    async def test_zero_matches_is_a_count_of_zero_not_null(self, plugin):
        """0 means "the search ran and matched nothing" -- a known, complete
        count. null means "unmeasured". Conflating them makes a complete
        answer look like a sample."""
        _wire(plugin, {"results": [], "query": "zzzznotaword", "page": "0"})
        result = await plugin.execute_tool(
            "search_code", {"query": "zzzznotaword"}
        )
        assert _structured(result)["total_count"] == 0

    @pytest.mark.asyncio
    async def test_page_past_the_end(self, plugin):
        """Zero rows past the last page must NOT report a total of zero.

        Verified against the live API: page 999 of "dog license" returns no
        results but still reports totalResultCount 618. Hardcoding 0 on the
        empty branch said "no such law exists" about a query with 618
        matching sections -- a false complete count, which is worse than
        null. Caught by running the zero-result table against real data
        rather than mocks.
        """
        _wire(
            plugin,
            {
                "results": [],
                "query": "dog license",
                "page": "999",
                "maxPages": 31,
                "totalResultCount": 618,
            },
        )
        result = await plugin.execute_tool(
            "search_code", {"query": "dog license", "page": 999}
        )
        structured = _structured(result)
        validate(structured, SEARCH_OUTPUT_SCHEMA)

        assert structured["rows"] == []
        assert structured["total_count"] == 618, (
            "a page past the end reported a complete count of zero for a "
            "query that has 618 matches"
        )
        assert any(
            c["code"] == CAVEAT_PAGE_PAST_END for c in structured["caveats"]
        )

    @pytest.mark.asyncio
    async def test_structure_node_with_no_children(self, plugin):
        _wire(
            plugin,
            {
                "results": {
                    "guid": "49401966",
                    "number": "17.15.010",
                    "title": "Dog license required",
                    "children": [],
                }
            },
        )
        result = await plugin.execute_tool(
            "browse_structure", {"guid": "49401966"}
        )
        structured = _structured(result)
        validate(structured, STRUCTURE_OUTPUT_SCHEMA)
        assert structured["children"] == []
        assert any(
            c["code"] == CAVEAT_NODE_HAS_NO_CHILDREN
            for c in structured["caveats"]
        )

    @pytest.mark.asyncio
    async def test_toc_with_no_entries(self, plugin):
        _wire(plugin, {"guid": "ROOT", "title": "Code", "children": []})
        result = await plugin.execute_tool("get_table_of_contents", {})
        structured = _structured(result)
        validate(structured, TOC_OUTPUT_SCHEMA)
        assert structured["children"] == []


# --------------------------------------------------------------------------
# C3/C5 -- shape and honesty
# --------------------------------------------------------------------------


SEARCH_PAYLOAD = {
    "query": "dog license",
    "page": "0",
    "maxPages": 31,
    "totalResultCount": 618,
    "results": [
        {
            "id": "49401992",
            "number": "§ 17.15.060",
            "title": "Special purpose <em class='highlight'>license</em>.",
            "text": "A <em class='highlight'>dog</em> license is required.",
            "type": "code",
            "url": "https://ecode360.com/49401992",
        },
        {
            "id": "49401966",
            "number": "§ 17.15.010",
            "title": "Dog license required.",
            "text": "Any dog over four months.",
            "type": "code",
            "url": "https://ecode360.com/49401966",
        },
    ],
}


class TestSearchStructure:
    @pytest.mark.asyncio
    async def test_conforms_and_carries_chainable_guids(self, plugin):
        _wire(plugin, SEARCH_PAYLOAD)
        result = await plugin.execute_tool("search_code", {"query": "dog license"})
        structured = _structured(result)
        validate(structured, SEARCH_OUTPUT_SCHEMA)

        assert structured["total_count"] == 618
        assert [r["guid"] for r in structured["rows"]] == ["49401992", "49401966"]
        # The round-trip structuredContent exists to remove: a caller can go
        # straight to get_section without regexing a number out of prose.
        for row in structured["rows"]:
            assert row["url"] == f"https://ecode360.com/{row['guid']}"

    @pytest.mark.asyncio
    async def test_highlight_markup_is_stripped_from_structured_fields(
        self, plugin
    ):
        _wire(plugin, SEARCH_PAYLOAD)
        result = await plugin.execute_tool("search_code", {"query": "dog license"})
        for row in _structured(result)["rows"]:
            assert "<em" not in (row["title"] or "")
            assert "<em" not in (row["snippet"] or "")

    @pytest.mark.asyncio
    async def test_ref_comes_from_the_api_not_the_rendered_text(self, plugin):
        """Citation drift: the ref must be what the API returned for THAT
        row, never something re-derived from the prose."""
        _wire(plugin, SEARCH_PAYLOAD)
        result = await plugin.execute_tool("search_code", {"query": "dog license"})
        refs = [r["ref"] for r in _structured(result)["rows"]]
        assert refs == ["§ 17.15.060", "§ 17.15.010"]

    @pytest.mark.asyncio
    async def test_total_count_is_null_when_upstream_omits_it(self, plugin):
        """null means unmeasured; it must not be faked from the page size."""
        payload = dict(SEARCH_PAYLOAD)
        payload.pop("totalResultCount")
        _wire(plugin, payload)
        result = await plugin.execute_tool("search_code", {"query": "dog"})
        assert _structured(result)["total_count"] is None

    @pytest.mark.asyncio
    async def test_more_pages_caveat_on_a_truncated_result_set(self, plugin):
        _wire(plugin, SEARCH_PAYLOAD)
        result = await plugin.execute_tool("search_code", {"query": "dog license"})
        structured = _structured(result)
        codes = [c["code"] for c in structured["caveats"]]
        assert CAVEAT_MORE_PAGES in codes

    @pytest.mark.asyncio
    async def test_no_more_pages_caveat_on_the_last_page(self, plugin):
        payload = dict(SEARCH_PAYLOAD, page="30", maxPages=31)
        _wire(plugin, payload)
        result = await plugin.execute_tool(
            "search_code", {"query": "dog license", "page": 30}
        )
        structured = _structured(result)
        validate(structured, SEARCH_OUTPUT_SCHEMA)
        codes = [c["code"] for c in structured["caveats"]]
        assert CAVEAT_MORE_PAGES not in codes


class TestSectionStructure:
    @pytest.mark.asyncio
    async def test_conforms_and_carries_citation_metadata(self, plugin):
        _wire(
            plugin,
            {
                "id": "49401966",
                "number": "§ 17.15.010",
                "title": "Dog license required",
                "type": "code",
                "content": "Any dog over four months must be licensed.",
                "children": [],
            },
        )
        result = await plugin.execute_tool("get_section", {"guid": "49401966"})
        structured = _structured(result)
        validate(structured, SECTION_OUTPUT_SCHEMA)

        # The server instructions REQUIRE citing the section number and the
        # ecode360 link; making both machine-readable is the difference
        # between a model that always cites correctly and one that usually
        # does.
        assert structured["ref"] == "§ 17.15.010"
        assert structured["url"] == "https://ecode360.com/49401966"
        assert "four months" in structured["text"]

    @pytest.mark.asyncio
    async def test_long_body_is_clipped_in_text_but_not_in_structured(
        self, plugin
    ):
        """Truncating the machine-readable half would defeat its purpose."""
        body = "x" * (MAX_CONTENT_CHARS + 5000)
        _wire(
            plugin,
            {
                "id": "1",
                "number": "1.1",
                "title": "Long",
                "content": body,
                "children": [],
            },
        )
        result = await plugin.execute_tool("get_section", {"guid": "1"})
        structured = _structured(result)
        validate(structured, SECTION_OUTPUT_SCHEMA)

        assert len(structured["text"]) > MAX_CONTENT_CHARS
        assert len(_text(result)) < len(structured["text"])
        assert any(
            c["code"] == CAVEAT_RENDERED_TEXT_TRUNCATED
            for c in structured["caveats"]
        ), "silent truncation: the cap is not reported"

    @pytest.mark.asyncio
    async def test_pdf_section_is_flagged(self, plugin):
        _wire(
            plugin,
            {
                "id": "2",
                "number": "2.1",
                "title": "Schedule",
                "pdf": True,
                "content": "",
                "children": [],
            },
        )
        result = await plugin.execute_tool("get_section", {"guid": "2"})
        structured = _structured(result)
        validate(structured, SECTION_OUTPUT_SCHEMA)
        assert structured["is_pdf"] is True
        assert any(
            c["code"] == CAVEAT_BODY_IS_ATTACHMENT for c in structured["caveats"]
        )

    @pytest.mark.asyncio
    async def test_attachment_section_is_flagged(self, plugin):
        _wire(
            plugin,
            {
                "id": "3",
                "number": "3.1",
                "title": "Appendix",
                "attachments": {"AppendixA.pdf": "/x"},
                "content": "",
                "children": [],
            },
        )
        result = await plugin.execute_tool("get_section", {"guid": "3"})
        structured = _structured(result)
        validate(structured, SECTION_OUTPUT_SCHEMA)
        assert structured["has_attachment"] is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "title", ["(Reserved)", "Repealed", "Superseded by Ord. 2020-1"]
    )
    async def test_not_in_force_is_a_banner_not_a_footnote(self, plugin, title):
        """A repealed or reserved section changes how the text below must be
        read, so the warning has to arrive before the text, not after it."""
        _wire(
            plugin,
            {
                "id": "4",
                "number": "4.1",
                "title": title,
                "content": "Body text here.",
                "children": [],
            },
        )
        result = await plugin.execute_tool("get_section", {"guid": "4"})
        structured = _structured(result)
        validate(structured, SECTION_OUTPUT_SCHEMA)

        assert any(
            c["code"] == CAVEAT_SECTION_NOT_IN_FORCE for c in structured["caveats"]
        )
        text = _text(result)
        assert text.index("Note:") < text.index("Body text here.")

    @pytest.mark.asyncio
    async def test_repealed_in_the_body_does_not_cry_wolf(self, plugin):
        """"repealed" is ordinary language in the body of sections that are
        perfectly in force, so only the heading is checked."""
        _wire(
            plugin,
            {
                "id": "5",
                "number": "5.1",
                "title": "Amendments",
                "content": "Ordinance 1999-12 is hereby repealed.",
                "children": [],
            },
        )
        result = await plugin.execute_tool("get_section", {"guid": "5"})
        codes = [c["code"] for c in _structured(result)["caveats"]]
        assert CAVEAT_SECTION_NOT_IN_FORCE not in codes


class TestOutlineStructure:
    @pytest.mark.asyncio
    async def test_children_are_a_tree_not_a_table(self, plugin):
        _wire(
            plugin,
            {
                "results": {
                    "guid": "100",
                    "number": "Chapter 17.15",
                    "title": "Animal Control",
                    "type": "chapter",
                    "children": [
                        {
                            "guid": "49401966",
                            "number": "17.15.010",
                            "title": "Dog license required",
                            "type": "section",
                        },
                        {
                            "guid": "49401992",
                            "number": "17.15.060",
                            "title": "Special purpose license",
                            "type": "section",
                        },
                    ],
                }
            },
        )
        result = await plugin.execute_tool("browse_structure", {"guid": "100"})
        structured = _structured(result)
        validate(structured, STRUCTURE_OUTPUT_SCHEMA)

        assert structured["node"]["guid"] == "100"
        assert [c["guid"] for c in structured["children"]] == [
            "49401966",
            "49401992",
        ]
        assert structured["children"][0]["ref"] == "17.15.010"

    @pytest.mark.asyncio
    async def test_bare_guid_children_still_yield_entries(self, plugin):
        """The API sometimes returns just an id in place of a child object."""
        _wire(
            plugin,
            {"results": {"guid": "100", "children": ["111", "222"]}},
        )
        result = await plugin.execute_tool("browse_structure", {"guid": "100"})
        structured = _structured(result)
        validate(structured, STRUCTURE_OUTPUT_SCHEMA)
        assert [c["guid"] for c in structured["children"]] == ["111", "222"]

    @pytest.mark.asyncio
    async def test_toc_fallback_is_reported_as_a_caveat(self, plugin):
        """The fallback used to be a parenthetical the caller had to notice."""
        calls = []

        async def fake_get(path, params=None):
            calls.append(path)
            if path.endswith("/code/toc"):
                return httpx.Response(
                    status_code=404,
                    json={"message": "no toc"},
                    request=httpx.Request("GET", "https://api.ecode360.com/v1/x"),
                )
            return _respond(
                {
                    "guid": "ROOT",
                    "title": "Anchorage Code",
                    "children": [
                        {"guid": "1", "number": "Title 1", "title": "General"}
                    ],
                }
            )

        plugin.client = MagicMock()
        plugin.client.get = AsyncMock(side_effect=fake_get)

        result = await plugin.execute_tool("get_table_of_contents", {})
        structured = _structured(result)
        validate(structured, TOC_OUTPUT_SCHEMA)
        assert any(
            c["code"] == CAVEAT_TOC_ENDPOINT_UNAVAILABLE
            for c in structured["caveats"]
        )


# --------------------------------------------------------------------------
# C4 -- prose and structure cannot drift
# --------------------------------------------------------------------------


class TestCaveatsCannotDrift:
    """Generated from ONE list, so every structured message is in the text."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "tool,args,payload",
        [
            ("search_code", {"query": "dog license"}, SEARCH_PAYLOAD),
            (
                "get_section",
                {"guid": "4"},
                {
                    "id": "4",
                    "number": "4.1",
                    "title": "(Reserved)",
                    "content": "Body.",
                    "children": [],
                },
            ),
            (
                "browse_structure",
                {"guid": "49401966"},
                {"results": {"guid": "49401966", "children": []}},
            ),
        ],
    )
    async def test_every_structured_caveat_appears_in_the_text(
        self, plugin, tool, args, payload
    ):
        _wire(plugin, payload)
        result = await plugin.execute_tool(tool, args)
        structured = _structured(result)
        text = _text(result)

        assert structured["caveats"], f"{tool} produced no caveats to check"
        for caveat in structured["caveats"]:
            assert caveat["message"] in text, (
                f"{tool}: caveat {caveat['code']} is in structuredContent "
                f"but not in the human-readable text"
            )


class TestStructuredContentReachesTheWire:
    """The plumbing, end to end: outputSchema in tools/list and
    structuredContent in tools/call."""

    @pytest.mark.asyncio
    async def test_tools_list_emits_output_schema_and_title(self, plugin):
        from core.plugin_manager import PluginManager

        manager = PluginManager({"plugins": {}})
        manager._initialized = True
        manager.plugins = {"ecode": plugin}
        manager.tools = {
            f"ecode__{t.name}": ("ecode", t.name) for t in plugin.get_tools()
        }

        tools = {t["name"]: t for t in manager.get_all_tools()}
        assert tools["ecode__search_code"]["title"] == "Search Code"
        assert "outputSchema" in tools["ecode__search_code"]
        # A tool that declares none must not emit an empty one.
        assert "outputSchema" not in tools["ecode__get_customer_info"]

    @pytest.mark.asyncio
    async def test_tools_call_emits_structured_content(self, plugin):
        from core.mcp_server import MCPServer
        from core.plugin_manager import PluginManager

        _wire(plugin, SEARCH_PAYLOAD)
        manager = MagicMock(spec=PluginManager)
        manager.execute_tool = AsyncMock(
            return_value=await plugin.execute_tool(
                "search_code", {"query": "dog license"}
            )
        )
        server = MCPServer(manager)

        response = await server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "ecode__search_code",
                    "arguments": {"query": "dog license"},
                },
            }
        )

        assert "structuredContent" in response["result"]
        validate(response["result"]["structuredContent"], SEARCH_OUTPUT_SCHEMA)
        # And it survives JSON serialization onto the wire.
        json.dumps(response)
