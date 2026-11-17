"""Long sections: a larger text cap and get_section(offset=...).

From the 2026-11-17 prod-log review: 5 of 151 attorney get_section calls
were cut at the old 14,000-character cap (the DUI section § 9.28.020 is
27,585 characters, the fine schedule § 9.48.130 18,745) and the model
never went back for the rest. The complete text was in structuredContent
all along, but a client that shows the model only the text block never
sees it. So the text block now carries far more, and says exactly how to
read on when it still is not everything.
"""

import re
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from jsonschema import validate

import plugins.ecode.plugin as ecode_mod
from plugins.ecode.config_schema import EcodePluginConfig
from plugins.ecode.plugin import (
    BATCH_SECTION_CHARS,
    BATCH_TOTAL_CHARS,
    CAVEAT_RENDERED_TEXT_TRUNCATED,
    MAX_CONTENT_CHARS,
    SECTION_OUTPUT_SCHEMA,
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


def _section(guid, n_lines, number="§\xa09.28.020", title="DUI."):
    """A section whose body is numbered 100-character lines, so any slice
    of it says where in the section it came from."""
    lines = [f"L{i:05d} " + "x" * 93 for i in range(n_lines)]
    return {
        "id": guid,
        "number": number,
        "title": title,
        "type": "section",
        "content": "\n".join(lines),
        "children": [],
    }


def _plugin(*sections):
    by_guid = {s["id"]: s for s in sections}

    async def fake_get(path, params=None, **_kw):
        guid = path.rsplit("/", 1)[-1]
        if guid in by_guid:
            return httpx.Response(200, json=by_guid[guid], request=REQ)
        return httpx.Response(404, json={"message": "no"}, request=REQ)

    p = EcodePlugin(CONFIG)
    p.plugin_config = EcodePluginConfig(**CONFIG)
    p.client = MagicMock()
    p.client.get = AsyncMock(side_effect=fake_get)
    return p


async def _read(plugin, guid, **args):
    result = await plugin.execute_tool("get_section", {"guid": guid, **args})
    assert result.success, result.error_message
    sc = result.structured_content
    validate(sc, SECTION_OUTPUT_SCHEMA)
    text = result.content[0]["text"]
    for caveat in sc["caveats"]:
        assert caveat["message"] in text
    return sc, text


def _line_numbers(text):
    return [int(n) for n in re.findall(r"^L(\d{5}) ", text, re.MULTILINE)]


def _truncation(sc):
    return [
        c["message"] for c in sc["caveats"]
        if c["code"] == CAVEAT_RENDERED_TEXT_TRUNCATED
    ]


class TestLargerCap:
    def test_cap_covers_the_sections_cut_short_in_prod(self):
        # § 9.28.020 was 27,585 characters and Chapter 7.25 31,586.
        assert MAX_CONTENT_CHARS >= 32_000

    @pytest.mark.asyncio
    async def test_a_28k_section_is_returned_whole(self):
        section = _section("1", 280)  # about 28,000 characters
        sc, text = await _read(_plugin(section), "1")
        assert _truncation(sc) == []
        assert _line_numbers(text) == list(range(280))


class TestContinuation:
    @pytest.mark.asyncio
    async def test_first_call_says_how_to_read_on(self):
        section = _section("77", 900)  # about 90,000 characters
        sc, text = await _read(_plugin(section), "77")

        (message,) = _truncation(sc)
        assert message.startswith("INCOMPLETE")
        assert "get_section with guid 77" in message
        offset = int(re.search(r"offset=(\d+)", message).group(1))
        assert 0 < offset <= MAX_CONTENT_CHARS
        assert f"of {len(sc['text']):,}" in message
        # structuredContent still carries everything.
        assert _line_numbers(sc["text"]) == list(range(900))
        assert _line_numbers(text)[0] == 0

    @pytest.mark.asyncio
    async def test_following_the_offsets_reads_every_line_exactly_once(self):
        section = _section("77", 900)
        plugin = _plugin(section)
        seen = []
        offset = 0
        for _ in range(10):
            sc, text = await _read(plugin, "77", offset=offset)
            seen.extend(_line_numbers(text))
            messages = _truncation(sc)
            match = messages and re.search(r"offset=(\d+)", messages[0])
            if not match:
                break
            offset = int(match.group(1))
        else:
            pytest.fail("never reached the end of the section")

        assert seen == list(range(900))

    @pytest.mark.asyncio
    async def test_a_part_ends_on_a_line_break(self):
        section = _section("77", 900)
        sc, text = await _read(_plugin(section), "77")
        body = text[text.index("L00000"):]
        assert body.endswith("\n")
        assert len(body.splitlines()[-1]) == 100  # the last line is whole

    @pytest.mark.asyncio
    async def test_last_part_says_it_is_the_end(self):
        section = _section("77", 900)
        total = len(section["content"])
        sc, text = await _read(_plugin(section), "77", offset=total - 500)
        (message,) = _truncation(sc)
        assert "the end of this section" in message
        assert "offset=" not in message
        assert _line_numbers(text)[-1] == 899

    @pytest.mark.asyncio
    async def test_header_and_link_are_on_every_part(self):
        section = _section("77", 900)
        _, text = await _read(_plugin(section), "77", offset=50_000)
        assert "DUI." in text
        assert "https://ecode360.com/77" in text

    @pytest.mark.asyncio
    async def test_a_body_with_no_line_breaks_is_still_cut_and_continued(self):
        section = _section("5", 1)
        section["content"] = "y" * (MAX_CONTENT_CHARS + 1234)
        plugin = _plugin(section)
        sc, _ = await _read(plugin, "5")
        offset = int(re.search(r"offset=(\d+)", _truncation(sc)[0]).group(1))
        assert offset == MAX_CONTENT_CHARS
        sc, text = await _read(plugin, "5", offset=offset)
        assert text.endswith("y" * 1234)
        assert "the end of this section" in _truncation(sc)[0]


class TestOffsetErrors:
    @pytest.mark.parametrize(
        "offset,expect",
        [
            (-1, "offset must be 0 or greater"),
            ("ten", "offset must be an integer"),
            (10**7, "is past the end of this section"),
        ],
    )
    @pytest.mark.asyncio
    async def test_bad_offset_is_explained(self, offset, expect):
        plugin = _plugin(_section("1", 10))
        result = await plugin.execute_tool(
            "get_section", {"guid": "1", "offset": offset}
        )
        assert result.success is False
        assert expect in result.error_message

    @pytest.mark.asyncio
    async def test_offset_zero_is_the_default(self):
        plugin = _plugin(_section("1", 10))
        _, with_zero = await _read(plugin, "1", offset=0)
        _, without = await _read(plugin, "1")
        assert with_zero == without


class TestBatchAllowance:
    async def _batch(self, *sections):
        plugin = _plugin(*sections)
        result = await plugin.execute_tool(
            "get_sections", {"sections": [s["id"] for s in sections]}
        )
        assert result.success, result.error_message
        validate(result.structured_content, SECTIONS_OUTPUT_SCHEMA)
        return result

    @pytest.mark.asyncio
    async def test_two_long_sections_share_the_allowance(self):
        # The 2026-11-15 prod call: § 9.28.020 and § 9.28.022 together.
        result = await self._batch(_section("1", 280), _section("2", 280))
        text = result.content[0]["text"]
        for section in result.structured_content["sections"]:
            assert section["caveats"] == []
        assert _line_numbers(text) == list(range(280)) * 2

    @pytest.mark.asyncio
    async def test_ten_sections_get_the_floor_each(self):
        assert BATCH_TOTAL_CHARS // 10 <= BATCH_SECTION_CHARS
        sections = [_section(str(i), 120) for i in range(1, 11)]
        result = await self._batch(*sections)
        text = result.content[0]["text"]
        assert len(text) < BATCH_TOTAL_CHARS + 10 * 1500
        first = result.structured_content["sections"][0]
        (caveat,) = first["caveats"]
        # The pointer names the single-section tool and this guid.
        assert "get_section with guid 1 and offset=" in caveat["message"]

    @pytest.mark.asyncio
    async def test_a_cut_batch_section_continues_with_get_section(self):
        sections = [_section(str(i), 120) for i in range(1, 11)]
        plugin = _plugin(*sections)
        result = await plugin.execute_tool(
            "get_sections", {"sections": [s["id"] for s in sections]}
        )
        message = result.structured_content["sections"][2]["caveats"][0][
            "message"
        ]
        offset = int(re.search(r"offset=(\d+)", message).group(1))
        shown = _line_numbers(
            result.content[0]["text"].split("=== 3 ===")[1].split("===")[0]
        )
        _, rest = await _read(plugin, "3", offset=offset)
        assert shown + _line_numbers(rest) == list(range(120))


def test_tool_schema_declares_offset():
    plugin = EcodePlugin(CONFIG)
    plugin.plugin_config = EcodePluginConfig(**CONFIG)
    tool = next(t for t in plugin.get_tools() if t.name == "get_section")
    assert tool.input_schema["properties"]["offset"]["minimum"] == 0
    assert tool.input_schema["required"] == ["guid"]
    assert "offset" in tool.description
    assert ecode_mod.MAX_CONTENT_CHARS == MAX_CONTENT_CHARS
