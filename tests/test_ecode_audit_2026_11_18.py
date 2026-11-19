"""Regression tests for the attorney audit of 2026-11-18.

Each class is one audit item, built on the example the audit gave. Node
fixtures are trimmed copies of what the eCode360 API returned that day.
"""

import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import yaml
from jsonschema import validate

from plugins.ecode.config_schema import EcodePluginConfig
from plugins.ecode.plugin import (
    CAVEAT_DEFINITION_LOOKUP,
    CAVEAT_LIST_POSITION_INFERRED,
    CAVEAT_NO_MATCH_IN_CODE,
    CAVEAT_NODE_TOO_LARGE,
    CAVEAT_OUTLINE_DEPTH_LIMITED,
    CAVEAT_RELAXED_MATCH,
    SEARCH_OUTPUT_SCHEMA,
    SECTION_OUTPUT_SCHEMA,
    TOC_OUTPUT_SCHEMA,
    EcodePlugin,
)

REPO = Path(__file__).resolve().parent.parent


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


def _route(plugin, handler):
    """Wire the upstream to `handler(path, params) -> json`."""

    async def get(path, params=None, **_):
        return httpx.Response(
            200,
            json=handler(path, params or {}),
            request=httpx.Request("GET", "https://api.ecode360.com" + path),
        )

    plugin.client = MagicMock()
    plugin.client.get = AsyncMock(side_effect=get)


def _codes(sc):
    return [c["code"] for c in sc["caveats"]]


def _sub(number, content, children=None, title="Unfair labor practices."):
    return {"id": "9" + re.sub(r"\W", "", number) + str(len(content)),
            "number": number, "title": title, "content": content,
            "type": "subsection", "subtype": "litem",
            "children": children or [], "attachments": {}, "images": []}


# ── 1. get_table_of_contents ───────────────────────────────────────────


def _toc_node(guid, label, number, title, children=None, kind="chapter"):
    return {"type": kind, "guid": guid, "label": label, "number": number,
            "title": title, "children": children}


TOC = _toc_node("AN6998", "", "", "Municipality of Anchorage, AK", kind="code", children=[
    _toc_node("49245794", "Part", "I", "PART I: CHARTER", kind="division", children=[
        _toc_node("48977220", "Title", "CHA", "Charter", children=[
            _toc_node("48977252", "ARTICLE", "IV", "THE ASSEMBLY", kind="article", children=[
                _toc_node("48977253", "Section", "Section 4.01",
                          "Power, composition and apportionment.", kind="section"),
                _toc_node("48977255", "Section", "Section 4.02", "Qualifications.",
                          kind="section"),
            ]),
        ]),
    ]),
    _toc_node("49277145", "", "II", "PART II: ANCHORAGE MUNICIPAL CODE", kind="division", children=[
        _toc_node("49388774", "Title", "1", "General Provisions", children=[
            _toc_node("49388775", "Chapter", "1.05", "ANCHORAGE MUNICIPAL CODE",
                      kind="article", children=[
                          _toc_node("49388776", "Section", "§ 1.05.010", "How Code designated.",
                                    kind="section")]),
        ]),
    ]),
])


class TestTableOfContents:
    @pytest.mark.asyncio
    async def test_returns_parts_titles_and_chapters_but_not_sections(self, plugin):
        _route(plugin, lambda path, params: TOC)
        result = await plugin.execute_tool("get_table_of_contents", {})
        sc, text = result.structured_content, result.content[0]["text"]
        validate(sc, TOC_OUTPUT_SCHEMA)

        part = sc["children"][0]
        title = part["children"][0]
        article = title["children"][0]
        assert (part["guid"], title["guid"], article["guid"]) == (
            "49245794", "48977220", "48977252")
        # Chapters are the last level: their sections are counted, not listed.
        assert "children" not in article
        assert article["children_not_listed"] == 2
        assert "48977253" not in text and "Qualifications" not in text
        for guid in ("49245794", "48977220", "48977252", "49388775"):
            assert guid in text
        assert "Sections are not listed" in sc["summary"]
        assert sc["summary"] in text

    def test_description_and_instructions_say_what_it_returns(self, plugin):
        tool = next(t for t in plugin.get_tools() if t.name == "get_table_of_contents")
        assert "section level" not in tool.description
        assert "three levels" in tool.description
        assert "Sections and body text are NOT listed" in tool.description
        instructions = yaml.safe_load(
            (REPO / "config.yaml").read_text(encoding="utf-8"))["instructions"]
        assert "parts, titles and chapters (three levels), not sections" in " ".join(
            instructions.split())

    @pytest.mark.asyncio
    async def test_a_one_level_outline_says_so(self, plugin):
        shallow = dict(TOC, children=[dict(c, children=None) for c in TOC["children"]])
        _route(plugin, lambda path, params: shallow)
        result = await plugin.execute_tool("get_table_of_contents", {})
        assert CAVEAT_OUTLINE_DEPTH_LIMITED in _codes(result.structured_content)


# ── 2. one consistently labelled set of lengths ────────────────────────


def _big_title():
    section = lambda i: {  # noqa: E731
        "id": f"5{i:04d}", "number": f"§ 21.01.{i:03d}", "title": f"Section {i}.",
        "content": f"Section {i}. " + "x" * 900, "type": "section",
        "children": [], "attachments": {}, "images": []}
    return {"id": "49419884", "number": "21", "title": "Land Use Planning",
            "content": "", "type": "container", "attachments": {}, "images": [],
            "children": [section(i) for i in range(400)]}


class TestLengthReporting:
    @pytest.mark.asyncio
    async def test_too_large_node_labels_each_length(self, plugin):
        node = _big_title()
        whole = sum(len(c["content"]) for c in node["children"])
        _route(plugin, lambda path, params: node)
        plugin._code_last_updated = AsyncMock(return_value=None)
        result = await plugin.execute_tool("get_section", {"guid": "49419884"})
        sc, text = result.structured_content, result.content[0]["text"]
        validate(sc, SECTION_OUTPUT_SCHEMA)

        length = sc["length"]
        assert length["whole_node_chars"] == whole
        assert length["text_chars"] == len(sc["text"]) < whole
        assert length["shown_from"] == 0 and length["shown_to"] < length["text_chars"]

        note = next(c["message"] for c in sc["caveats"]
                    if c["code"] == CAVEAT_NODE_TOO_LARGE)
        assert "Three different lengths apply" in note
        assert f"about {whole:,} characters of code text" in note
        assert f"{length['text_chars']:,} characters as formatted here" in note
        assert f"characters 1-{length['shown_to']:,} of those {length['text_chars']:,}" in note
        assert note in text

    @pytest.mark.asyncio
    async def test_offset_past_end_error_names_both_lengths(self, plugin):
        node = _big_title()
        whole = sum(len(c["content"]) for c in node["children"])
        _route(plugin, lambda path, params: node)
        plugin._code_last_updated = AsyncMock(return_value=None)
        ok = await plugin.execute_tool("get_section", {"guid": "49419884"})
        returned = ok.structured_content["length"]["text_chars"]
        bad = await plugin.execute_tool(
            "get_section", {"guid": "49419884", "offset": 900000})
        assert not bad.success
        assert f"{returned:,} characters this server returns" in bad.error_message
        assert f"about {whole:,} characters of code text" in bad.error_message

    @pytest.mark.asyncio
    async def test_ordinary_section_has_no_whole_node_length(self, plugin):
        _route(plugin, lambda path, params: CHARTER_401)
        plugin._code_last_updated = AsyncMock(return_value=None)
        result = await plugin.execute_tool("get_section", {"guid": "48977253"})
        length = result.structured_content["length"]
        assert length["whole_node_chars"] is None
        assert length["shown_to"] == length["text_chars"] == len(
            result.structured_content["text"])


# ── 3. search_code: a softer "no results", with follow-ups ─────────────

DEFINITION_HIT = {"id": "49247275", "number": "3.70.010{18}",
                  "title": '"Labor agreement"',
                  "text": '"Labor agreement" means a collective bargaining',
                  "type": "code"}
DEFINITION_NODE = {"id": "49247275", "number": "{18}", "title": '"Labor agreement"',
                   "content": '"Labor agreement" means a collective bargaining '
                              "agreement that is the result of an exchange of "
                              "mutual promises.",
                   "type": "definition", "children": [], "attachments": {},
                   "images": []}


def _labor_upstream(path, params):
    if "/code/content/" in path:
        return DEFINITION_NODE
    query = params.get("query", "")
    if query == '"Labor agreement means"':
        hits = []
    elif query.startswith("title:"):
        hits = [DEFINITION_HIT]
    else:
        hits = [DEFINITION_HIT,
                {"id": "49703896", "number": "25.35.020{4}",
                 "title": '"Labor peace agreement"', "text": "means an agreement",
                 "type": "code"}]
    return {"results": hits, "page": "0", "maxPages": 1 if hits else 0,
            "totalResultCount": len(hits)}


class TestNoResultsMessage:
    @pytest.mark.asyncio
    async def test_labor_agreement_means(self, plugin):
        _route(plugin, _labor_upstream)
        result = await plugin.execute_tool(
            "search_code", {"query": '"Labor agreement means"'})
        sc, text = result.structured_content, result.content[0]["text"]
        validate(sc, SEARCH_OUTPUT_SCHEMA)

        # The phrase itself still matched nothing, and is reported that way.
        assert sc["rows"] == [] and sc["total_count"] == 0
        assert sc["query"] == '"Labor agreement means"'
        assert _codes(sc) == [CAVEAT_NO_MATCH_IN_CODE, CAVEAT_DEFINITION_LOOKUP,
                              CAVEAT_RELAXED_MATCH]

        # a. the message no longer claims the text is absent
        first = sc["caveats"][0]["message"]
        assert "matched this query as written" in first
        assert "does not appear" not in first
        assert "punctuation" in first

        # b. the definition is found and shown
        definition = sc["related_definitions"][0]
        assert definition["defined_in"] == "3.70.010"
        assert definition["exact_match"] is True
        assert "means a collective bargaining agreement" in definition["text"]
        assert "means a collective bargaining agreement" in text
        assert "find_definition('Labor agreement')" in sc["caveats"][1]["message"]

        # c. the relaxed retry is labelled and kept apart from `rows`
        relaxed = sc["relaxed_match"]
        assert relaxed["query"] == '"Labor" AND "agreement" AND "means"'
        assert [r["guid"] for r in relaxed["rows"]] == ["49247275", "49703896"]
        assert "NOT matches for the phrase" in sc["caveats"][2]["message"]
        assert "RELAXED MATCHES" in text
        for caveat in sc["caveats"]:
            assert caveat["message"] in text

    @pytest.mark.parametrize(
        "query",
        ['"bus lane" AND transit', "bus lane zzqx", '"bus*"', '"a"',
         '"one two three four five six seven eight nine"'],
    )
    @pytest.mark.asyncio
    async def test_no_follow_up_unless_one_plain_phrase(self, plugin, query):
        _route(plugin, lambda path, params: {
            "results": [], "page": "0", "maxPages": 0, "totalResultCount": 0})
        result = await plugin.execute_tool("search_code", {"query": query})
        assert plugin.client.get.call_count == 1
        assert "relaxed_match" not in result.structured_content

    @pytest.mark.asyncio
    async def test_failed_follow_up_does_not_sink_the_result(self, plugin):
        def upstream(path, params):
            if params.get("query") == '"Labor agreement means"':
                return {"results": [], "page": "0", "maxPages": 0,
                        "totalResultCount": 0}
            raise httpx.ConnectError("boom")

        _route(plugin, upstream)
        result = await plugin.execute_tool(
            "search_code", {"query": '"Labor agreement means"'})
        assert result.success
        assert _codes(result.structured_content) == [CAVEAT_NO_MATCH_IN_CODE]

    @pytest.mark.asyncio
    async def test_no_follow_up_past_the_first_page(self, plugin):
        _route(plugin, lambda path, params: {
            "results": [], "page": "3", "maxPages": 0, "totalResultCount": 0})
        await plugin.execute_tool(
            "search_code", {"query": '"Labor agreement means"', "page": 3})
        assert plugin.client.get.call_count == 1


# ── 4. text after a list is rendered after the list ────────────────────

HISTORY_401 = ("(AO No. 2006-154, prop. 10, 4-17-2007; AO No. 2019-82, "
               "Prop. 12, 10-8-2019/election of 4-7-2020)")
CHARTER_401 = {
    "id": "48977253", "number": "Section 4.01",
    "title": "Power, composition and apportionment.", "type": "section",
    "attachments": {}, "images": [],
    "content": (
        "Power, composition and apportionment. The legislative power of "
        "Anchorage is vested in an assembly of 12 members. The assembly "
        "shall determine and declare by resolution whether or not it is "
        "malapportioned within two months from: If the assembly determines "
        "that it is malapportioned, it shall, within six months of the "
        "determination, reapportion itself in the manner provided by law. "
        + HISTORY_401),
    "children": [_sub(
        "(1)", "Adoption of a final state redistricting plan under art. VI, "
        "sec. 10, Constitution of the State of Alaska;",
        title="Power, composition and apportionment.")],
}

HISTORY_140 = "(AO No. 69-75; AO No. 88-131(S); AO No. 2023-63, § 1, 6-6-2023)"
SECTION_370140 = {
    "id": "49247442", "number": "§ 3.70.140", "title": "Unfair labor practices.",
    "type": "section", "attachments": {}, "images": [],
    "content": "Unfair labor practices. " + HISTORY_140,
    "children": [
        _sub("A.", "Prohibited acts by municipality. The municipality or its "
             "agents may not:",
             [_sub("1.", "Interfere, restrain or coerce an employee."),
              _sub("2.", "Dominate or interfere with an organization.")]),
        _sub("B.", "Prohibited acts by employees and employee representatives. "
             "An employee organization or bargaining representative or its "
             "agents or employees may not: Notwithstanding the above, it is "
             "not an unfair labor practice to charge a non-member reasonable "
             "costs.",
             [_sub("1.", "Restrain or coerce:",
                   [_sub("a.", "An employee in the exercise of the rights.")]),
              _sub("6.", "Engage in any illegal effort to interfere.")]),
        _sub("C.", "Complaints; informal conciliation."),
    ],
}

AMCR_370120B = {
    "id": "48977680", "number": "§ R3.70.120", "title": "Petitions for certification.",
    "type": "section", "attachments": {}, "images": [],
    "content": "Petitions for certification. (AR No. 97-180, § 1, 8-19-1997)",
    "children": [_sub(
        "B.", "A petition for certification shall be in writing and signed. "
        "If the petition concerns positions in an existing bargaining unit, "
        "it must be filed: Petitions concerning new positions or new "
        "bargaining units may be filed at any time.",
        [_sub("1.", 'Within the "open window" period; or',
              title="Petitions for certification."),
         _sub("2.", "Following expiration of a collective bargaining agreement.",
              title="Petitions for certification.")],
        title="Petitions for certification.")],
}


async def _read(plugin, node):
    _route(plugin, lambda path, params: node)
    plugin._code_last_updated = AsyncMock(return_value=None)
    result = await plugin.execute_tool("get_section", {"guid": node["id"]})
    assert result.success, result.error_message
    validate(result.structured_content, SECTION_OUTPUT_SCHEMA)
    return result.structured_content, result.content[0]["text"]


def _in_order(text, *pieces):
    at = -1
    for piece in pieces:
        found = text.find(piece, at + 1)
        assert found > at, f"{piece!r} is missing or out of order"
        at = found


class TestListOrder:
    @pytest.mark.asyncio
    async def test_charter_4_01(self, plugin):
        sc, text = await _read(plugin, CHARTER_401)
        for body in (sc["text"], text):
            _in_order(body, "within two months from:", "(1) Adoption of a final",
                      "If the assembly determines", HISTORY_401)
        assert "from: If the assembly" not in sc["text"]
        # The API returns ONE list item here and so does the tool.
        assert sc["text"].count("(1)") == 1 and "(2)" not in sc["text"]
        assert CAVEAT_LIST_POSITION_INFERRED in _codes(sc)
        note = next(c["message"] for c in sc["caveats"]
                    if c["code"] == CAVEAT_LIST_POSITION_INFERRED)
        assert "https://ecode360.com/48977253" in note and note in text

    @pytest.mark.asyncio
    async def test_3_70_140_subsection_b(self, plugin):
        sc, _ = await _read(plugin, SECTION_370140)
        _in_order(sc["text"], "employees may not:", "1. Restrain or coerce:",
                  "a. An employee", "6. Engage in any illegal effort",
                  "Notwithstanding the above", "C. Complaints", HISTORY_140)
        assert "may not: Notwithstanding" not in sc["text"]

    @pytest.mark.asyncio
    async def test_amcr_3_70_120_subsection_b(self, plugin):
        sc, _ = await _read(plugin, AMCR_370120B)
        _in_order(sc["text"], "it must be filed:", '1. Within the "open window"',
                  "2. Following expiration", "Petitions concerning new positions",
                  "(AR No. 97-180")

    @pytest.mark.asyncio
    async def test_history_note_closes_the_section(self, plugin):
        sc, _ = await _read(plugin, SECTION_370140)
        assert sc["text"].rstrip().endswith(HISTORY_140)
        assert sc["text"].count(HISTORY_140) == 1

    @pytest.mark.parametrize(
        "content",
        [
            # a heading written with a colon is not a list lead-in
            "Administration: An RFA shall be administered by the department.",
            "Protest: The assembly shall act on applications by resolution.",
            # an ordinary lead-in ends with its colon; nothing follows
            "A petition filed by the municipality shall contain the following:",
            "The authorization forms or cards must",
        ],
    )
    @pytest.mark.asyncio
    async def test_text_is_left_alone_when_nothing_follows_a_list(self, plugin, content):
        node = dict(SECTION_370140, children=[
            _sub("A.", content, [_sub("1.", "First item."), _sub("2.", "Second.")])])
        sc, _ = await _read(plugin, node)
        _in_order(sc["text"], content, "1. First item.", "2. Second.")
        assert CAVEAT_LIST_POSITION_INFERRED not in _codes(sc)

    @pytest.mark.asyncio
    async def test_two_possible_positions_are_flagged_not_guessed(self, plugin):
        content = ("The board shall, in every case that comes before it, do the "
                   "following: Give notice to each party as required and then "
                   "act in this order: Decide the matter within thirty days.")
        node = dict(SECTION_370140, children=[
            _sub("A.", content, [_sub("1.", "First item.")])])
        sc, _ = await _read(plugin, node)
        _in_order(sc["text"], content, "1. First item.")
        note = next(c["message"] for c in sc["caveats"]
                    if c["code"] == CAVEAT_LIST_POSITION_INFERRED)
        assert "could not be worked out" in note


# ── 5. the section heading appears once ────────────────────────────────


class TestHeadingNotRepeated:
    @pytest.mark.asyncio
    async def test_3_70_140(self, plugin):
        sc, text = await _read(plugin, SECTION_370140)
        assert text.count("Unfair labor practices.") == 1
        assert "§ 3.70.140 Unfair labor practices." in text.splitlines()
        assert "Unfair labor practices" not in sc["text"]
        assert sc["title"] == "Unfair labor practices."
        lines = [line.strip() for line in sc["text"].splitlines()]
        assert lines[0] == ("A. Prohibited acts by municipality. The "
                            "municipality or its agents may not:")
        assert lines[1] == "1. Interfere, restrain or coerce an employee."
        assert "a. An employee in the exercise of the rights." in lines

    @pytest.mark.asyncio
    async def test_body_does_not_open_with_the_heading(self, plugin):
        sc, _ = await _read(plugin, CHARTER_401)
        assert sc["text"].startswith("The legislative power of Anchorage")

    @pytest.mark.asyncio
    async def test_sections_of_a_chapter_keep_their_own_headings(self, plugin):
        chapter = {
            "id": "49247250", "number": "3.70", "title": "EMPLOYEE RELATIONS",
            "type": "container", "content": "", "attachments": {}, "images": [],
            "children": [SECTION_370140, AMCR_370120B],
        }
        sc, _ = await _read(plugin, chapter)
        assert sc["text"].count("§ 3.70.140 Unfair labor practices.") == 1
        assert sc["text"].count("Unfair labor practices.") == 1
        assert sc["text"].count("§ R3.70.120 Petitions for certification.") == 1
        assert sc["text"].count("Petitions for certification.") == 1
        _in_order(sc["text"], "§ 3.70.140 Unfair labor practices.",
                  "A. Prohibited acts by municipality.", HISTORY_140,
                  "§ R3.70.120 Petitions for certification.")

    @pytest.mark.asyncio
    async def test_a_definition_keeps_its_term(self, plugin):
        """A definition's heading IS its term; find_definition must not
        strip it from the text it reports."""
        _route(plugin, _labor_upstream)
        result = await plugin.execute_tool("find_definition", {"term": "Labor agreement"})
        text = result.structured_content["definitions"][0]["text"]
        assert text.startswith('"Labor agreement" means')


# ── 6. instructions never name a tool the server does not register ─────

TOOL_NAME_RE = re.compile(
    r"\b(?:search|get|find|browse|read|list|query|fetch|lookup)_[a-z]+(?:_[a-z]+)*\b")


class TestInstructionsNameOnlyRegisteredTools:
    def _registered(self, plugin):
        return {t.name for t in plugin.get_tools()}

    @pytest.mark.parametrize("config", ["config.yaml", "config-ecode.yaml"])
    def test_server_instructions(self, plugin, config):
        instructions = yaml.safe_load(
            (REPO / config).read_text(encoding="utf-8"))["instructions"]
        named = set(TOOL_NAME_RE.findall(instructions))
        assert "find_references" in named and "search_code" in named
        assert named <= self._registered(plugin), (
            f"instructions name unregistered tool(s): "
            f"{sorted(named - self._registered(plugin))}")

    def test_tool_descriptions(self, plugin):
        registered = self._registered(plugin)
        for tool in plugin.get_tools():
            named = set(TOOL_NAME_RE.findall(tool.description))
            assert named <= registered, (tool.name, sorted(named - registered))

    def test_every_registered_tool_is_dispatched(self, plugin):
        source = (REPO / "plugins" / "ecode" / "plugin.py").read_text(encoding="utf-8")
        for name in self._registered(plugin):
            assert f'if tool_name == "{name}":' in source, name
