"""find_references: where the code cites a section (1.10.0).

From the 2026-11-18 prod-log review: one traffic conversation ran about 15
searches for section numbers as text ('"9.14.040" AND "fine schedule"' -> 0)
looking for fines. The fine schedule (§ 9.48.130) cites SUBSECTIONS
("§ 9.14.040A.3.a $200.00"), which a quoted search for the number does not
match; the unquoted wildcard 9.14.040* does. The tool runs that search,
drops the section's own node and lists fine/penalty sections first.
"""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from jsonschema import validate

from plugins.ecode.config_schema import EcodePluginConfig
from plugins.ecode.plugin import (
    CAVEAT_MORE_REFERENCES,
    CAVEAT_NO_REFERENCES_FOUND,
    REFERENCES_OUTPUT_SCHEMA,
    EcodePlugin,
)


def _hit(guid, number, title, text="snippet", **extra):
    return {"id": guid, "number": number, "title": title, "text": text,
            "type": "section", **extra}


# The live answer for 9.14.040*, in the order the API returned it.
HITS = [
    _hit("49349786", "§ 9.14.040", "Traffic control signal legend.",
         "( CAC 9.14.040 ; AO No. 78-72"),
    _hit("49349985", "§ 9.20.010",
         "Obedience to traffic control devices and traffic regulations.",
         "as provided in section 9.14.040 ."),
    _hit("49351636", "§ 9.48.130", "Traffic offenses fine schedule.",
         "Stop for steady red signal § 9.14.040A.3.a $200.00"),
    _hit("49672291", "§ CCT.020", "Ordinance Disposition Table.",
         "23 § 9.14.040 24 § 9.14.050"),
]


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


def _wire(plugin, results, total=None, max_pages=1):
    plugin.client = MagicMock()
    plugin.client.get = AsyncMock(
        return_value=httpx.Response(
            200,
            json={
                "results": results,
                "page": "0",
                "maxPages": max_pages,
                "totalResultCount": len(results) if total is None else total,
            },
            request=httpx.Request("GET", "https://api.ecode360.com/v1/x"),
        )
    )


async def _refs(plugin, args):
    result = await plugin.execute_tool("find_references", args)
    assert result.success, result.error_message
    sc = result.structured_content
    validate(sc, REFERENCES_OUTPUT_SCHEMA)
    return sc, result.content[0]["text"]


def _sent_query(plugin):
    call = plugin.client.get.call_args
    params = call.kwargs.get("params") or (call.args[1] if len(call.args) > 1 else {})
    return next(v for v in params.values() if isinstance(v, str) and "*" in v)


class TestFindReferences:
    @pytest.mark.asyncio
    async def test_fine_schedule_first_self_dropped_table_last(self, plugin):
        _wire(plugin, HITS)
        sc, text = await _refs(plugin, {"section": "9.14.040"})
        assert [(r["ref"], r["kind"]) for r in sc["references"]] == [
            ("§ 9.48.130", "fine_or_penalty"),
            ("§ 9.20.010", "section"),
            ("§ CCT.020", "reference_table"),
        ]
        assert "$200.00" in sc["references"][0]["snippet"]
        assert sc["section"] == "9.14.040"
        assert sc["query"] == "9.14.040*"
        assert sc["total_count"] == 4
        assert sc["caveats"] == []
        assert "$200.00" in text
        assert _sent_query(plugin) == "9.14.040*"

    @pytest.mark.parametrize(
        "given, number",
        [
            ("§ 9.14.040", "9.14.040"),
            ("Section 9.14.040.", "9.14.040"),
            ("9.28.019B.2", "9.28.019B.2"),
            ("R3.70.120", "R3.70.120"),
        ],
    )
    @pytest.mark.asyncio
    async def test_accepted_forms(self, plugin, given, number):
        _wire(plugin, [])
        sc, _ = await _refs(plugin, {"section": given})
        assert sc["section"] == number
        assert _sent_query(plugin) == number + "*"

    @pytest.mark.parametrize(
        "given, number",
        [
            # Parentheses are grouping in a search query: "9.28.019(B)*"
            # was read as "9.28.019 OR B*" and matched 8,289 sections.
            # The code itself writes "9.28.019B.2".
            ("9.28.019(B)", "9.28.019B"),
            ("9.28.019(B)(2)", "9.28.019B.2"),
            ("§ 9.14.040(A)(3)(a)", "9.14.040A.3.a"),
            ("9.28.019B(2)", "9.28.019B.2"),
            ("9.28.019.B.2", "9.28.019B.2"),
            ("9.28.019(B).", "9.28.019B"),
        ],
    )
    @pytest.mark.asyncio
    async def test_parenthesised_subsection_is_rewritten(self, plugin, given, number):
        _wire(plugin, [])
        sc, _ = await _refs(plugin, {"section": given})
        assert sc["section"] == number
        sent = _sent_query(plugin)
        assert sent == number + "*"
        assert "(" not in sent and ")" not in sent

    @pytest.mark.parametrize(
        "given", ["9.28.019(B", "9.28.019B)", "9.28.019()", "9.28.019((B))x)"]
    )
    @pytest.mark.asyncio
    async def test_a_suffix_that_cannot_be_rewritten_is_refused(self, plugin, given):
        _wire(plugin, [])
        result = await plugin.execute_tool("find_references", {"section": given})
        assert result.success is False
        assert "section number" in result.error_message
        # Nothing was searched for.
        assert plugin.client.get.await_count == 0

    @pytest.mark.asyncio
    async def test_subsection_drops_every_node_of_its_section(self, plugin):
        _wire(plugin, [
            _hit("1", "§ 9.28.019", "Valid operator's license required."),
            _hit("2", "9.28.019B{1}", "Sub node"),
            _hit("3", "§ 9.48.010", "Penalties."),
        ])
        sc, _ = await _refs(plugin, {"section": "9.28.019B"})
        assert [r["guid"] for r in sc["references"]] == ["3"]
        assert sc["references"][0]["kind"] == "fine_or_penalty"

    @pytest.mark.asyncio
    async def test_fine_schedule_named_reference_table_is_a_fine(self, plugin):
        """Live 2026-11-18: § 8.05.025 carries the fine for § 8.35.410."""
        _wire(plugin, [_hit(
            "5", "§ 8.05.025",
            "Minor offense fine schedule; misdemeanor reference table.",
            "Intoxicated persons on roadway § 8.35.410 $500.00",
        )])
        sc, _ = await _refs(plugin, {"section": "8.35.410"})
        assert sc["references"][0]["kind"] == "fine_or_penalty"

    @pytest.mark.asyncio
    async def test_pdf_hit_is_a_document(self, plugin):
        _wire(plugin, [_hit("7", "", "Fee table", pdf=True,
                            url="https://live.ecode360.com/attachment/x.pdf")])
        sc, _ = await _refs(plugin, {"section": "9.14.040"})
        assert sc["references"][0]["kind"] == "document"
        assert sc["references"][0]["is_pdf"] is True

    @pytest.mark.asyncio
    async def test_only_self_hits_is_an_explained_empty_success(self, plugin):
        _wire(plugin, HITS[:1])
        sc, text = await _refs(plugin, {"section": "9.14.040"})
        assert sc["references"] == []
        assert [c["code"] for c in sc["caveats"]] == [CAVEAT_NO_REFERENCES_FOUND]
        assert "general penalty provision" in text

    @pytest.mark.asyncio
    async def test_empty_result_does_not_say_page_1_of_0(self, plugin):
        """Prod 2026-11-18: the upstream sends maxPages 0 with no results."""
        _wire(plugin, [], max_pages=0)
        sc, text = await _refs(plugin, {"section": "99.99.999"})
        assert "page 1 of 1" in sc["summary"]
        assert "of 0" not in text

    @pytest.mark.asyncio
    async def test_more_pages_are_announced(self, plugin):
        _wire(plugin, HITS, total=45, max_pages=3)
        sc, _ = await _refs(plugin, {"section": "9.14.040"})
        assert [c["code"] for c in sc["caveats"]] == [CAVEAT_MORE_REFERENCES]
        assert "page=1" in sc["caveats"][0]["message"]

    @pytest.mark.parametrize(
        "args",
        [
            {},
            {"section": "  "},
            {"section": "9.28"},
            {"section": "Title 9"},
            {"section": '"9.14.040" AND fine'},
            {"section": "9.14.040*"},
            {"section": "9." * 30},
            {"section": "9.14.040", "page": -1},
        ],
    )
    @pytest.mark.asyncio
    async def test_bad_input_is_refused_before_any_upstream_call(self, plugin, args):
        _wire(plugin, HITS)
        result = await plugin.execute_tool("find_references", args)
        assert not result.success
        plugin.client.get.assert_not_called()
