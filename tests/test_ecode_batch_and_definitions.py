"""get_sections (batch read) and find_definition.

Both exist because of what 14 days of prod logs showed (2026-11-15):
models read chapters one get_section at a time -- 79 bursts, the longest
19 calls -- and hunt for definitions with queries like '"Minor offense"
means' that match thousands of sections or none.

Payload shapes below are the live AN6998 ones, trimmed.
"""

import httpx
import pytest
from jsonschema import validate
from unittest.mock import AsyncMock, MagicMock

import plugins.ecode.plugin as ecode_mod
from plugins.ecode.config_schema import EcodePluginConfig
from plugins.ecode.plugin import (
    CAVEAT_DEFINITION_TEXT_FROM_SNIPPET,
    CAVEAT_DUPLICATE_REQUESTS,
    CAVEAT_MORE_DEFINITIONS,
    CAVEAT_MULTIPLE_DEFINITIONS,
    CAVEAT_NO_DEFINITION_FOUND,
    CAVEAT_RENDERED_TEXT_TRUNCATED,
    CAVEAT_RESOLVED_FROM_SECTION_NUMBER,
    CAVEAT_SECTION_UNAVAILABLE,
    DEFINITION_OUTPUT_SCHEMA,
    MAX_BATCH_SECTIONS,
    MAX_DEFINITIONS,
    SECTIONS_OUTPUT_SCHEMA,
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


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch):
    monkeypatch.setattr(ecode_mod, "RETRY_BACKOFF_S", 0.0)


def _json(body, status=200):
    return httpx.Response(status_code=status, json=body, request=REQ)


def _section(guid, number, title, content="Body text."):
    return {
        "id": guid,
        "number": number,
        "title": title,
        "type": "section",
        "content": content,
        "children": [],
        "images": [],
        "attachments": {},
    }


def _routed_plugin(content=None, searches=None, failing=None):
    """A plugin whose upstream is routed by path, not by call order.

    Batch requests run concurrently, so a positional side_effect list
    would make these tests depend on scheduling.
    """
    content = content or {}
    searches = searches or {}
    failing = failing or {}
    calls = []

    async def fake_get(path, params=None, **_kw):
        calls.append((path, params))
        if path.endswith("/search"):
            query = params["query"]
            if query in failing:
                return _json({"message": "boom"}, failing[query])
            return _json(
                searches.get(query, {"totalResultCount": 0, "results": []})
            )
        guid = path.rsplit("/", 1)[-1]
        if guid in failing:
            return _json({"message": "boom"}, failing[guid])
        if guid in content:
            return _json(content[guid])
        return _json({"message": f"Could not find Content: {guid}"}, 404)

    p = EcodePlugin(CONFIG)
    p.plugin_config = EcodePluginConfig(**CONFIG)
    p.client = MagicMock()
    p.client.get = AsyncMock(side_effect=fake_get)
    p.calls = calls
    return p


SNOW = _section("111", "§\xa024.80.090", "Removal of snow and ice.")
DEFS = _section("222", "§\xa024.80.100", "Definitions.")
SEARCH_24_80_100 = {
    "totalResultCount": 2,
    "results": [
        {"id": "222", "number": "§\xa024.80.100", "title": "Definitions."},
        {"id": "999", "number": "§\xa024.80.090", "title": "Cites it."},
    ],
}


# ── get_sections ───────────────────────────────────────────────────────


class TestGetSections:
    @pytest.mark.asyncio
    async def test_returns_every_section_in_request_order(self):
        p = _routed_plugin(
            content={"111": SNOW, "222": DEFS},
            searches={"24.80.100": SEARCH_24_80_100},
        )
        result = await p.execute_tool(
            "get_sections", {"sections": ["24.80.100", "111"]}
        )

        assert result.success is True
        sc = result.structured_content
        validate(sc, SECTIONS_OUTPUT_SCHEMA)
        assert [s["requested"] for s in sc["sections"]] == ["24.80.100", "111"]
        assert [s["guid"] for s in sc["sections"]] == ["222", "111"]
        assert all(s["found"] and s["error"] is None for s in sc["sections"])
        assert sc["summary"].startswith("2 of 2")

    @pytest.mark.asyncio
    async def test_a_number_is_resolved_and_says_so(self):
        p = _routed_plugin(
            content={"222": DEFS}, searches={"24.80.100": SEARCH_24_80_100}
        )
        sc = (
            await p.execute_tool("get_sections", {"sections": ["§ 24.80.100"]})
        ).structured_content

        codes = [c["code"] for c in sc["sections"][0]["caveats"]]
        assert CAVEAT_RESOLVED_FROM_SECTION_NUMBER in codes

    @pytest.mark.asyncio
    async def test_one_missing_section_does_not_fail_the_others(self):
        p = _routed_plugin(content={"111": SNOW})
        result = await p.execute_tool(
            "get_sections", {"sections": ["111", "99.99.999", "424242"]}
        )

        assert result.success is True
        sc = result.structured_content
        validate(sc, SECTIONS_OUTPUT_SCHEMA)
        found = [s["found"] for s in sc["sections"]]
        assert found == [True, False, False]
        assert "99.99.999" in sc["sections"][1]["error"]
        assert sc["sections"][1]["text"] is None
        codes = [c["code"] for c in sc["caveats"]]
        assert codes.count(CAVEAT_SECTION_UNAVAILABLE) == 2
        assert sc["summary"].startswith("1 of 3")

    @pytest.mark.asyncio
    async def test_upstream_failure_on_one_is_reported_in_place(self):
        p = _routed_plugin(content={"111": SNOW}, failing={"222": 500})
        result = await p.execute_tool(
            "get_sections", {"sections": ["111", "222"]}
        )

        assert result.success is True
        second = result.structured_content["sections"][1]
        assert second["found"] is False
        assert "HTTP 500" in second["error"]

    @pytest.mark.asyncio
    async def test_all_missing_is_an_error_result(self):
        p = _routed_plugin()
        result = await p.execute_tool(
            "get_sections", {"sections": ["424242", "99.99.999"]}
        )

        assert result.success is False
        assert "None of the requested sections" in result.error_message
        assert "424242" in result.error_message

    @pytest.mark.asyncio
    async def test_all_failing_upstream_surfaces_the_upstream_error(self):
        p = _routed_plugin(failing={"111": 500, "222": 500})
        result = await p.execute_tool(
            "get_sections", {"sections": ["111", "222"]}
        )

        assert result.success is False
        assert "HTTP 500" in result.error_message

    @pytest.mark.asyncio
    async def test_repeats_are_fetched_once_and_reported(self):
        p = _routed_plugin(content={"111": SNOW})
        sc = (
            await p.execute_tool(
                "get_sections", {"sections": ["111", " 111 ", "111"]}
            )
        ).structured_content

        assert len(sc["sections"]) == 1
        assert len(p.calls) == 1
        assert CAVEAT_DUPLICATE_REQUESTS in [c["code"] for c in sc["caveats"]]

    @pytest.mark.asyncio
    async def test_a_bare_string_is_taken_as_one_section(self):
        p = _routed_plugin(content={"111": SNOW})
        result = await p.execute_tool("get_sections", {"sections": "111"})

        assert result.success is True
        assert len(result.structured_content["sections"]) == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "bad",
        [None, [], "", [""], [{"guid": "1"}], [True], 7],
    )
    async def test_bad_sections_argument_is_a_caller_error(self, bad):
        p = _routed_plugin(content={"111": SNOW})
        args = {} if bad is None else {"sections": bad}
        result = await p.execute_tool("get_sections", args)

        assert result.success is False
        assert "sections" in result.error_message
        assert p.calls == []

    @pytest.mark.asyncio
    async def test_too_many_sections_is_rejected_before_any_fetch(self):
        p = _routed_plugin()
        many = [str(1000 + i) for i in range(MAX_BATCH_SECTIONS + 1)]
        result = await p.execute_tool("get_sections", {"sections": many})

        assert result.success is False
        assert f"at most {MAX_BATCH_SECTIONS}" in result.error_message
        assert p.calls == []

    @pytest.mark.asyncio
    async def test_long_body_is_capped_in_text_but_whole_in_structured(self):
        body = "x" * (ecode_mod.BATCH_SECTION_CHARS + 500)
        long = _section("111", "§\xa01.01.010", "Long.", content=body)
        result = await p_execute(long)

        section = result.structured_content["sections"][0]
        assert section["text"] == body
        codes = [c["code"] for c in section["caveats"]]
        assert CAVEAT_RENDERED_TEXT_TRUNCATED in codes
        assert len(result.content[0]["text"]) < len(body)

    @pytest.mark.asyncio
    async def test_every_caveat_message_appears_in_the_text(self):
        p = _routed_plugin(
            content={"111": SNOW, "222": DEFS},
            searches={"24.80.100": SEARCH_24_80_100},
        )
        result = await p.execute_tool(
            "get_sections", {"sections": ["111", "24.80.100", "424242", "111"]}
        )

        text = result.content[0]["text"]
        sc = result.structured_content
        messages = [c["message"] for c in sc["caveats"]]
        for s in sc["sections"]:
            messages += [c["message"] for c in s["caveats"]]
        assert messages
        for message in messages:
            assert message in text

    @pytest.mark.asyncio
    async def test_batch_shares_one_call_budget(self, monkeypatch):
        """A spent budget fails the whole batch readably; it never lets ten
        requests each run a full timeout past the Lambda's."""
        monkeypatch.setattr(ecode_mod, "TOOL_CALL_BUDGET_S", -1.0)
        p = _routed_plugin(content={"111": SNOW, "222": DEFS})
        result = await p.execute_tool(
            "get_sections", {"sections": ["111", "222"]}
        )

        assert result.success is False
        assert "budget" in result.error_message
        assert p.calls == []


async def p_execute(section):
    p = _routed_plugin(content={section["id"]: section})
    return await p.execute_tool("get_sections", {"sections": [section["id"]]})


# ── find_definition ────────────────────────────────────────────────────


def _hit(guid, number, title, text="...excerpt..."):
    return {
        "id": guid,
        "number": number,
        "title": title,
        "text": text,
        "type": "code",
        "pdf": "False",
    }


def _definition(guid, title, content):
    return {
        "id": guid,
        "number": "",
        "title": title,
        "type": "definition",
        "content": content,
        "children": [],
    }


# Live shape of title:"vehicle" (2026-11-15): definition nodes numbered
# with a {n} suffix, mixed with an ordinary section and a related term.
VEHICLE_SEARCH = {
    "totalResultCount": 4,
    "maxPages": 1,
    "results": [
        _hit("700", "12.45.010{11}", '"Motor <em class="highlight">vehicles</em>"'),
        _hit("701", "9.04.010B{116}", '"<em class="highlight">Vehicle</em>"'),
        _hit("702", "§\xa09.30.010", "Vehicle equipment."),
        _hit("703", "21.15.040{463}", '"<em class="highlight">Vehicle</em>."'),
    ],
}
VEHICLE_CONTENT = {
    "700": _definition("700", '"Motor vehicles"', '"Motor vehicles" means A.'),
    "701": _definition("701", '"Vehicle"', '"Vehicle" means every device.'),
    "703": _definition("703", '"Vehicle."', "Vehicle. A device for zoning."),
}


class TestFindDefinition:
    @pytest.mark.asyncio
    async def test_returns_definition_nodes_exact_matches_first(self):
        p = _routed_plugin(
            content=VEHICLE_CONTENT,
            searches={'title:"vehicle"': VEHICLE_SEARCH},
        )
        result = await p.execute_tool("find_definition", {"term": "vehicle"})

        assert result.success is True
        sc = result.structured_content
        validate(sc, DEFINITION_OUTPUT_SCHEMA)
        assert [d["guid"] for d in sc["definitions"]] == ["701", "703", "700"]
        assert [d["exact_match"] for d in sc["definitions"]] == [
            True,
            True,
            False,
        ]
        assert sc["definitions"][0]["text"] == '"Vehicle" means every device.'
        assert sc["definitions"][0]["url"] == "https://ecode360.com/701"
        assert sc["total_count"] == 4

    @pytest.mark.asyncio
    async def test_defined_in_is_a_resolvable_section_number(self):
        p = _routed_plugin(
            content=VEHICLE_CONTENT,
            searches={'title:"vehicle"': VEHICLE_SEARCH},
        )
        sc = (
            await p.execute_tool("find_definition", {"term": "vehicle"})
        ).structured_content
        where = {d["guid"]: d["defined_in"] for d in sc["definitions"]}

        # The trailing subsection letter is dropped: "9.04.010B" is not a
        # number get_section can resolve, "9.04.010" is.
        assert where == {
            "701": "9.04.010",
            "703": "21.15.040",
            "700": "12.45.010",
        }
        for ref in where.values():
            assert EcodePlugin._section_ref(ref) == ref

    @pytest.mark.asyncio
    async def test_ordinary_sections_are_not_reported_as_definitions(self):
        p = _routed_plugin(
            content=VEHICLE_CONTENT,
            searches={'title:"vehicle"': VEHICLE_SEARCH},
        )
        sc = (
            await p.execute_tool("find_definition", {"term": "vehicle"})
        ).structured_content

        assert "702" not in [d["guid"] for d in sc["definitions"]]

    @pytest.mark.asyncio
    async def test_several_definitions_carry_the_scope_caveat(self):
        p = _routed_plugin(
            content=VEHICLE_CONTENT,
            searches={'title:"vehicle"': VEHICLE_SEARCH},
        )
        result = await p.execute_tool("find_definition", {"term": "vehicle"})
        sc = result.structured_content

        codes = [c["code"] for c in sc["caveats"]]
        assert CAVEAT_MULTIPLE_DEFINITIONS in codes
        for caveat in sc["caveats"]:
            assert caveat["message"] in result.content[0]["text"]

    @pytest.mark.asyncio
    async def test_no_definition_is_a_success_with_next_steps(self):
        only_section = {
            "totalResultCount": 1,
            "results": [_hit("702", "§\xa09.30.010", "Vehicle equipment.")],
        }
        p = _routed_plugin(searches={'title:"vehicle"': only_section})
        result = await p.execute_tool("find_definition", {"term": "vehicle"})

        assert result.success is True
        sc = result.structured_content
        validate(sc, DEFINITION_OUTPUT_SCHEMA)
        assert sc["definitions"] == []
        assert [c["code"] for c in sc["caveats"]] == [
            CAVEAT_NO_DEFINITION_FOUND
        ]
        assert '"vehicle" AND means' in sc["caveats"][0]["message"]

    @pytest.mark.asyncio
    async def test_quotes_in_the_term_cannot_alter_the_query(self):
        p = _routed_plugin()
        await p.execute_tool(
            "find_definition", {"term": '  "minor  offense" OR "x'}
        )

        queries = [params["query"] for _path, params in p.calls]
        assert queries == ['title:"minor offense OR x"']

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", [None, "", "   ", '""', 7, "w" * 101])
    async def test_bad_term_is_a_caller_error(self, bad):
        p = _routed_plugin()
        args = {} if bad is None else {"term": bad}
        result = await p.execute_tool("find_definition", args)

        assert result.success is False
        assert "term" in result.error_message
        assert p.calls == []

    @pytest.mark.asyncio
    async def test_failed_body_fetch_falls_back_to_the_snippet(self):
        p = _routed_plugin(
            content={"701": VEHICLE_CONTENT["701"]},
            searches={'title:"vehicle"': VEHICLE_SEARCH},
            failing={"703": 500},
        )
        result = await p.execute_tool("find_definition", {"term": "vehicle"})

        assert result.success is True
        sc = result.structured_content
        by_guid = {d["guid"]: d for d in sc["definitions"]}
        assert by_guid["703"]["text"] == "...excerpt..."
        assert by_guid["701"]["text"] == '"Vehicle" means every device.'
        codes = [c["code"] for c in sc["caveats"]]
        assert CAVEAT_DEFINITION_TEXT_FROM_SNIPPET in codes

    @pytest.mark.asyncio
    async def test_more_definitions_than_the_cap_is_reported(self):
        many = MAX_DEFINITIONS + 3
        search = {
            "totalResultCount": many,
            "maxPages": 1,
            "results": [
                _hit(str(800 + i), f"9.04.010B{{{i}}}", '"Lot"')
                for i in range(many)
            ],
        }
        content = {
            str(800 + i): _definition(str(800 + i), '"Lot"', f"Lot {i}.")
            for i in range(many)
        }
        p = _routed_plugin(content=content, searches={'title:"lot"': search})
        sc = (
            await p.execute_tool("find_definition", {"term": "lot"})
        ).structured_content

        assert len(sc["definitions"]) == MAX_DEFINITIONS
        assert CAVEAT_MORE_DEFINITIONS in [c["code"] for c in sc["caveats"]]
        # one search + one content fetch per definition shown, no more
        assert len(p.calls) == 1 + MAX_DEFINITIONS

    @pytest.mark.asyncio
    async def test_more_result_pages_is_reported(self):
        search = dict(VEHICLE_SEARCH, totalResultCount=201, maxPages=11)
        p = _routed_plugin(
            content=VEHICLE_CONTENT, searches={'title:"vehicle"': search}
        )
        sc = (
            await p.execute_tool("find_definition", {"term": "vehicle"})
        ).structured_content

        assert CAVEAT_MORE_DEFINITIONS in [c["code"] for c in sc["caveats"]]

    @pytest.mark.asyncio
    async def test_search_failure_is_an_error_result(self):
        p = _routed_plugin(failing={'title:"vehicle"': 500})
        result = await p.execute_tool("find_definition", {"term": "vehicle"})

        assert result.success is False
        assert "HTTP 500" in result.error_message
