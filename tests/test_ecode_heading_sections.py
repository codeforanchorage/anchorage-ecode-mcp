"""Heading sections, the history note of a long section, and argument
names for a batch read (1.22.0).

The building codes print a section heading as a section with no text and
its parts ("N.1", "N.2") as separate sections beside it in the chapter. A
read of the heading returned a bare header with nothing to say where the
text was, and browse_structure called it a leaf to be read with
get_section.

A long section's history note is printed at its end, so a response that
stops earlier did not show which ordinances amended the section.

get_sections refused a list passed under the name get_section uses.
"""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from jsonschema import validate

from plugins.ecode.config_schema import EcodePluginConfig
from plugins.ecode.plugin import (
    CAVEAT_NODE_HAS_NO_CHILDREN,
    CAVEAT_SECTION_TEXT_IN_PARTS,
    HISTORY_HEAD_CHARS,
    PARTS_SHOWN,
    SECTION_OUTPUT_SCHEMA,
    SECTIONS_OUTPUT_SCHEMA,
    STRUCTURE_OUTPUT_SCHEMA,
    EcodePlugin,
)
from tests.test_ecode_scoped_search import CONFIG, _node

REQ = httpx.Request("GET", "https://api.ecode360.com/v1/x")


def _json(body, status=200):
    return httpx.Response(status_code=status, json=body, request=REQ)


# A chapter laid out as the building codes are: the heading and its parts
# are siblings. § 23.99.1001 shares the heading's digits but is not a part.
TOC = {
    "guid": "AN6998",
    "type": "code",
    "label": "",
    "number": "",
    "title": "Municipality of Anchorage, AK",
    "children": [
        _node("t23", "Title", "23", "Building Codes", "chapter", "Ttl 23", [
            _node("ch2399", "Chapter", "23.99", "EXAMPLE CODE", "article",
                  "Ch 23.99", [
                _node("h100", "Section", "§\xa023.99.100", "Appeals."),
                _node("p1", "Section", "§\xa023.99.100.1", "Form of appeal."),
                _node("p2", "Section", "§\xa023.99.100.2", "Hearing."),
                _node("p21", "Section", "§\xa023.99.100.2.1", "Notice."),
                _node("s200", "Section", "§\xa023.99.200", "Reserved."),
                _node("s1001", "Section", "§\xa023.99.1001", "Fees."),
            ]),
        ]),
    ],
}

PARTS = [
    ("p1", "§ 23.99.100.1", "Form of appeal."),
    ("p2", "§ 23.99.100.2", "Hearing."),
    ("p21", "§ 23.99.100.2.1", "Notice."),
]


def _section(guid, number, title, content=""):
    return {"id": guid, "number": number, "title": title, "type": "section",
            "content": content, "children": [], "attachments": {},
            "images": []}


CONTENTS = {
    # As the API gives a heading: its title again as its content.
    "h100": _section("h100", "§\xa023.99.100", "Appeals.", "Appeals."),
    "p1": _section("p1", "§\xa023.99.100.1", "Form of appeal.",
                   "An appeal shall be in writing."),
    "s200": _section("s200", "§\xa023.99.200", "Reserved."),
}


def _plugin(contents=None, toc_status=200):
    contents = CONTENTS if contents is None else contents
    calls = []

    async def fake_get(path, params=None, **_kw):
        calls.append(path)
        if path.endswith("/code/toc"):
            return _json(TOC if toc_status == 200 else {"message": "boom"},
                         toc_status)
        guid = path.rsplit("/", 1)[-1]
        if "/code/content/" in path and guid in contents:
            return _json(contents[guid])
        if "/structure/" in path and guid in contents:
            node = contents[guid]
            return _json({"error": False, "results": {
                "guid": guid, "label": "Section", "number": node["number"],
                "title": node["title"], "type": "section",
                "parent": "ch2399", "children": []}})
        return _json({"message": "unexpected"}, 404)

    p = EcodePlugin(CONFIG)
    p.plugin_config = EcodePluginConfig(**CONFIG)
    p.client = MagicMock()
    p.client.get = AsyncMock(side_effect=fake_get)
    p.calls = calls
    return p


def _codes(structured):
    return [c["code"] for c in structured["caveats"]]


class TestHeadingSection:
    @pytest.mark.asyncio
    async def test_a_heading_with_no_text_lists_its_parts(self):
        p = _plugin()
        result = await p.execute_tool("get_section", {"guid": "h100"})

        assert result.success, result.error_message
        sc = result.structured_content
        validate(sc, SECTION_OUTPUT_SCHEMA)
        assert [(x["guid"], x["ref"], x["title"]) for x in sc["parts"]] == PARTS
        assert sc["parts"][0]["url"] == "https://ecode360.com/p1"
        assert CAVEAT_SECTION_TEXT_IN_PARTS in _codes(sc)
        text = result.content[0]["text"]
        assert "heading with no text of its own" in text
        assert "part: § 23.99.100.1 Form of appeal.  [guid: p1]" in text
        # A number that merely starts with the same digits is not a part.
        assert "23.99.1001" not in text

    @pytest.mark.asyncio
    async def test_a_heading_with_empty_content_lists_them_too(self):
        bare = _section("h100", "§\xa023.99.100", "Appeals.")
        p = _plugin({"h100": bare})
        result = await p.execute_tool("get_section", {"guid": "h100"})

        assert result.success, result.error_message
        assert len(result.structured_content["parts"]) == 3

    @pytest.mark.asyncio
    async def test_a_section_with_text_is_not_looked_up(self):
        p = _plugin()
        result = await p.execute_tool("get_section", {"guid": "p1"})

        assert result.success, result.error_message
        assert result.structured_content["parts"] == []
        assert CAVEAT_SECTION_TEXT_IN_PARTS not in _codes(
            result.structured_content)
        assert not any(c.endswith("/code/toc") for c in p.calls)

    @pytest.mark.asyncio
    async def test_an_empty_section_with_nothing_under_it_says_nothing(self):
        p = _plugin()
        result = await p.execute_tool("get_section", {"guid": "s200"})

        assert result.success, result.error_message
        assert result.structured_content["parts"] == []
        assert CAVEAT_SECTION_TEXT_IN_PARTS not in _codes(
            result.structured_content)
        assert "part:" not in result.content[0]["text"]

    @pytest.mark.asyncio
    async def test_an_outline_failure_does_not_fail_the_read(self):
        p = _plugin(toc_status=500)
        result = await p.execute_tool("get_section", {"guid": "h100"})

        assert result.success, result.error_message
        validate(result.structured_content, SECTION_OUTPUT_SCHEMA)
        assert result.structured_content["parts"] == []

    @pytest.mark.asyncio
    async def test_a_batch_lists_the_parts_of_a_heading_in_it(self):
        p = _plugin()
        result = await p.execute_tool(
            "get_sections", {"sections": ["h100", "p1", "nope"]})

        assert result.success, result.error_message
        sc = result.structured_content
        validate(sc, SECTIONS_OUTPUT_SCHEMA)
        heading, part, missing = sc["sections"]
        assert [x["guid"] for x in heading["parts"]] == ["p1", "p2", "p21"]
        assert part["parts"] == [] and missing["parts"] == []
        assert "part: § 23.99.100.2 Hearing.  [guid: p2]" in (
            result.content[0]["text"])

    def test_a_long_list_of_parts_is_shortened_in_the_text_only(self):
        p = _plugin()
        many = [{"guid": f"g{i}", "ref": f"§ 1.2.3.{i}", "title": None,
                 "url": f"https://ecode360.com/g{i}"}
                for i in range(PARTS_SHOWN + 5)]
        node = {**CONTENTS["h100"], "_parts": many}
        text, structured = p._format_content(node)

        assert len(structured["parts"]) == PARTS_SHOWN + 5
        assert text.count("\npart: ") == PARTS_SHOWN
        assert "and 5 more part(s)" in text


class TestBrowseHeadingSection:
    @pytest.mark.asyncio
    async def test_a_heading_shows_its_parts_instead_of_the_leaf_note(self):
        p = _plugin()
        result = await p.execute_tool("browse_structure", {"guid": "h100"})

        assert result.success, result.error_message
        sc = result.structured_content
        validate(sc, STRUCTURE_OUTPUT_SCHEMA)
        assert [x["guid"] for x in sc["parts"]] == ["p1", "p2", "p21"]
        assert _codes(sc) == [CAVEAT_SECTION_TEXT_IN_PARTS]
        text = result.content[0]["text"]
        assert "part: § 23.99.100.1 Form of appeal.  [guid: p1]" in text
        assert "leaf section" not in text

    @pytest.mark.asyncio
    async def test_a_real_leaf_keeps_the_leaf_note(self):
        p = _plugin()
        result = await p.execute_tool("browse_structure", {"guid": "p1"})

        assert result.success, result.error_message
        sc = result.structured_content
        validate(sc, STRUCTURE_OUTPUT_SCHEMA)
        assert sc["parts"] == []
        assert _codes(sc) == [CAVEAT_NODE_HAS_NO_CHILDREN]

    @pytest.mark.asyncio
    async def test_an_outline_failure_falls_back_to_the_leaf_note(self):
        p = _plugin(toc_status=500)
        result = await p.execute_tool("browse_structure", {"guid": "h100"})

        assert result.success, result.error_message
        assert _codes(result.structured_content) == [
            CAVEAT_NODE_HAS_NO_CHILDREN]


HISTORY = "(AO No. 91-182, 12-17-1991; AO No. 2025-36, § 1, 4-16-2025)"


def _long_section(guid, n_lines, history=HISTORY):
    lines = [f"L{i:05d} " + "x" * 93 for i in range(n_lines)]
    return _section(guid, "§\xa09.99.010", "Long.",
                    "\n".join(lines) + " " + history)


class TestHistoryOfACutSection:
    @pytest.mark.asyncio
    async def test_the_first_part_carries_the_history_note(self):
        p = _plugin({"long": _long_section("long", 900)})
        result = await p.execute_tool("get_section", {"guid": "long"})

        assert result.success, result.error_message
        text = result.content[0]["text"]
        head = text.split("L00000")[0]
        assert "INCOMPLETE" in head
        assert "history note (printed at the end of this section" in head
        assert ("AO No. 91-182, 12-17-1991; AO No. 2025-36, § 1, "
                "4-16-2025") in head
        # The note itself is past what is shown.
        assert HISTORY not in text

    @pytest.mark.asyncio
    async def test_a_middle_part_carries_it_and_the_last_does_not(self):
        p = _plugin({"long": _long_section("long", 900)})
        first = await p.execute_tool("get_section", {"guid": "long"})
        to = first.structured_content["length"]["shown_to"]
        middle = await p.execute_tool(
            "get_section", {"guid": "long", "offset": to})
        to = middle.structured_content["length"]["shown_to"]
        last = await p.execute_tool(
            "get_section", {"guid": "long", "offset": to})

        assert "history note (printed at the end" in middle.content[0]["text"]
        tail = last.content[0]["text"]
        assert "history note (printed at the end" not in tail
        assert HISTORY in tail

    @pytest.mark.asyncio
    async def test_a_section_shown_whole_does_not_repeat_the_note(self):
        p = _plugin({"short": _long_section("short", 3)})
        result = await p.execute_tool("get_section", {"guid": "short"})

        text = result.content[0]["text"]
        assert "history note (printed at the end" not in text
        assert text.count("AO No. 91-182") == 1

    @pytest.mark.asyncio
    async def test_a_very_long_note_keeps_its_newest_entries(self):
        entries = [f"AO No. {1960 + i}-{i}, § 1, 1-2-{1960 + i}"
                   for i in range(400)]
        history = "(" + "; ".join(entries) + ")"
        p = _plugin({"long": _long_section("long", 900, history)})
        result = await p.execute_tool("get_section", {"guid": "long"})

        line = next(row for row in result.content[0]["text"].splitlines()
                    if row.startswith("history note"))
        assert len(line) < HISTORY_HEAD_CHARS + 300
        assert entries[-1] in line and entries[0] not in line
        assert "oldest entries are left out" in line
        # Still oldest first among the ones shown.
        assert line.index(entries[-2]) < line.index(entries[-1])

    @pytest.mark.asyncio
    async def test_a_section_cut_in_a_batch_carries_it_too(self):
        p = _plugin({"long": _long_section("long", 1200),
                     "p1": CONTENTS["p1"]})
        result = await p.execute_tool(
            "get_sections", {"sections": ["long", "p1"]})

        assert result.success, result.error_message
        validate(result.structured_content, SECTIONS_OUTPUT_SCHEMA)
        assert "history note (printed at the end" in result.content[0]["text"]


class TestSectionsArgumentNames:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("arguments", [
        {"guids": ["h100", "p1"]},
        {"guids": '["h100", "p1"]'},
        {"sections": '["h100", "p1"]'},
        {"guid": ["h100", "p1"]},
        {"section": ["h100", "p1"]},
    ])
    async def test_other_spellings_of_the_list_are_taken(self, arguments):
        p = _plugin()
        result = await p.execute_tool("get_sections", arguments)

        assert result.success, result.error_message
        assert [s["requested"] for s in
                result.structured_content["sections"]] == ["h100", "p1"]

    @pytest.mark.asyncio
    async def test_a_single_guid_under_the_other_name_is_taken(self):
        p = _plugin()
        result = await p.execute_tool("get_sections", {"guid": "p1"})

        assert result.success, result.error_message
        assert len(result.structured_content["sections"]) == 1

    @pytest.mark.asyncio
    async def test_sections_wins_when_both_are_given(self):
        p = _plugin()
        result = await p.execute_tool(
            "get_sections", {"sections": ["p1"], "guids": ["h100"]})

        assert [s["requested"] for s in
                result.structured_content["sections"]] == ["p1"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("arguments", [{}, {"guids": []},
                                           {"sections": "[not json"}])
    async def test_nothing_usable_is_still_refused_or_not_found(
            self, arguments):
        p = _plugin()
        result = await p.execute_tool("get_sections", arguments)

        assert not result.success
