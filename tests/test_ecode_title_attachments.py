"""Attachments of a whole title, editor's notes set apart, and three
smaller fixes (1.20.0).

Charter Section 3.01 opens with an editor's note saying the Charter
Commission Commentary "is included as an attachment to this title". A
read returned `attachments: []`, and the note ran straight into the law.
Checked live: 33 sections carry such a note (the Title 21 use tables
among them), none is given a link by the content endpoint, and the PDFs
are found only by searching `title:attachment`.

Also here: a defined term was printed twice, "1 entries", and the library
record and the outline were fetched one after the other.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from jsonschema import validate

from plugins.ecode import plugin as ecode_mod
from plugins.ecode.config_schema import EcodePluginConfig
from plugins.ecode.plugin import (
    CAVEAT_TITLE_ATTACHMENTS,
    SECTION_OUTPUT_SCHEMA,
    SECTIONS_OUTPUT_SCHEMA,
    TITLE_ATTACHMENT_QUERY,
    EcodePlugin,
)
from tests.test_ecode_scoped_search import CONFIG, TOC

REQ = httpx.Request("GET", "https://api.ecode360.com/v1/x")
NOTE = (
    "Editor’s Notes – There are Charter Commission Commentary "
    "notes on this section. Said Commentary is included as an attachment "
    "to this title."
)
LAW = "The municipality may exercise all legislative powers."
BASE = "https://live.ecode360.com/attachment/339626/"
COMMENTARY = (
    BASE + "AN6998-CHAb%20Commission%20Commentary%20on%20Anchorage"
    "%20Municipal%20Charter.pdf"
)


def _json(body, status=200):
    return httpx.Response(status_code=status, json=body, request=REQ)


def _pdf_hit(title_index, filename, title):
    return {
        "id": f"{title_index}: {filename}",
        "number": None,
        "title": title,
        "url": BASE + filename,
        "type": "code",
        "pdf": True,
        "text": "",
    }


# What `title:attachment` returns: the PDFs, and sections whose heading
# merely contains the word.
ATTACHMENT_HITS = [
    {"id": "s1705010", "number": "§ 17.05.010{16}4",
     "title": '"Control by attachment"', "url": "https://x/1",
     "type": "code", "pdf": False, "text": ""},
    _pdf_hit("Ttl CHA", "AN6998-CHAb Commission Commentary on Anchorage "
             "Municipal Charter.pdf", "Attachment 2 - Commission..."),
    _pdf_hit("Ttl CHA", "AN6998-CHAa Appendix A.pdf",
             "Attachment 1 - Appendix A"),
    _pdf_hit("Ttl 21", "AN6998-021a Table 21.05-1 Table of Allowed "
             "Uses.pdf", "Attachment 1 - Table 21.05-1 Table..."),
]


def _section(guid, content, number="Section 17.05", title="Powers.",
             children=()):
    return {"id": guid, "number": number, "title": title, "type": "section",
            "content": content, "children": list(children),
            "attachments": {}, "images": []}


def _plugin(contents, search_status=200, toc_status=200):
    """`contents`: {guid: content JSON}."""
    calls = []

    async def fake_get(path, params=None, **_kw):
        calls.append(path if not params else f"{path}?{params.get('query')}")
        if path.endswith("/code/toc"):
            return _json(TOC, toc_status)
        if path.endswith("/search"):
            if search_status != 200:
                return _json({"message": "boom"}, search_status)
            return _json({"results": ATTACHMENT_HITS, "page": "0",
                          "maxPages": 1,
                          "totalResultCount": len(ATTACHMENT_HITS)})
        guid = path.rsplit("/", 1)[-1]
        if "/code/content/" in path and guid in contents:
            return _json(contents[guid])
        return _json({"message": "unexpected"}, 404)

    p = EcodePlugin(CONFIG)
    p.plugin_config = EcodePluginConfig(**CONFIG)
    p.client = MagicMock()
    p.client.get = AsyncMock(side_effect=fake_get)
    p.calls = calls
    return p


def _searches(p):
    return [c for c in p.calls if "/search" in c]


class TestTitleAttachments:
    @pytest.mark.asyncio
    async def test_a_section_with_the_note_lists_its_titles_pdfs(self):
        p = _plugin({"c1705": _section("c1705", f"Powers. {NOTE} {LAW}")})
        result = await p.execute_tool("get_section", {"guid": "c1705"})

        assert result.success, result.error_message
        sc = result.structured_content
        validate(sc, SECTION_OUTPUT_SCHEMA)
        # Only the Charter's two, in file order; names without the prefix.
        assert sc["title_attachments"] == [
            {"name": "Appendix A", "url": BASE + "AN6998-CHAa%20Appendix%20A.pdf"},
            {"name": "Commission Commentary on Anchorage Municipal Charter",
             "url": COMMENTARY},
        ]
        assert sc["attachments"] == []
        caveat = next(
            c for c in sc["caveats"] if c["code"] == CAVEAT_TITLE_ATTACHMENTS
        )
        assert "the Charter" in caveat["message"]
        assert "read_attachment" in caveat["message"]
        text = result.content[0]["text"]
        assert f"title attachment: Commission Commentary on Anchorage " \
               f"Municipal Charter — {COMMENTARY}" in text
        assert _searches(p) == [f"/customer/AN6998/search?{TITLE_ATTACHMENT_QUERY}"]

    @pytest.mark.asyncio
    async def test_the_url_is_one_read_attachment_accepts(self):
        p = _plugin({"c1705": _section("c1705", f"Powers. {NOTE}")})
        result = await p.execute_tool("get_section", {"guid": "c1705"})
        for item in result.structured_content["title_attachments"]:
            assert p._attachment_url_arg({"url": item["url"]})

    @pytest.mark.asyncio
    async def test_a_code_title_is_named_by_number(self):
        p = _plugin({"s2115040": _section(
            "s2115040",
            "Uses. Editor's Note — Table 21.05-1, Table of Allowed "
            "Uses, is included as an attachment to this title.",
            number="§ 21.15.040")})
        result = await p.execute_tool("get_section", {"guid": "s2115040"})
        sc = result.structured_content
        assert [a["name"] for a in sc["title_attachments"]] == [
            "Table 21.05-1 Table of Allowed Uses"
        ]
        assert "Title 21 (Land Use Planning)" in sc["caveats"][0]["message"]

    @pytest.mark.asyncio
    async def test_the_note_may_sit_in_a_subsection_and_be_plural(self):
        child = _section("x", "The maps Editor's note — Maps are "
                         "included as attachments to this title.  adopted "
                         "by the assembly.", number="(b)", title="")
        p = _plugin({"c1705": _section("c1705", "Powers.", children=[child])})
        result = await p.execute_tool("get_section", {"guid": "c1705"})
        assert len(result.structured_content["title_attachments"]) == 2

    @pytest.mark.asyncio
    async def test_no_note_means_no_lookup(self):
        p = _plugin({"c1705": _section("c1705", f"Powers. {LAW}")})
        result = await p.execute_tool("get_section", {"guid": "c1705"})

        assert result.success
        assert result.structured_content["title_attachments"] == []
        assert p.calls == ["/customer/AN6998/code/content/c1705"]
        assert "title attachment" not in result.content[0]["text"]

    @pytest.mark.asyncio
    async def test_the_list_is_kept_between_reads(self):
        p = _plugin({
            "c1705": _section("c1705", f"Powers. {NOTE}"),
            "s2115040": _section("s2115040", f"Uses. {NOTE}"),
        })
        await p.execute_tool("get_section", {"guid": "c1705"})
        result = await p.execute_tool(
            "get_sections", {"sections": ["s2115040", "c1705"]}
        )
        assert result.success
        validate(result.structured_content, SECTIONS_OUTPUT_SCHEMA)
        first, second = result.structured_content["sections"]
        assert len(first["title_attachments"]) == 1
        assert len(second["title_attachments"]) == 2
        assert len(_searches(p)) == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize("kwargs", [
        {"search_status": 500}, {"toc_status": 500},
    ])
    async def test_a_failed_lookup_never_fails_the_read(self, kwargs,
                                                        monkeypatch):
        monkeypatch.setattr(ecode_mod, "RETRY_BACKOFF_S", 0)
        p = _plugin({"c1705": _section("c1705", f"Powers. {NOTE} {LAW}")},
                    **kwargs)
        result = await p.execute_tool("get_section", {"guid": "c1705"})

        assert result.success, result.error_message
        assert result.structured_content["title_attachments"] == []
        assert LAW in result.structured_content["text"]

    @pytest.mark.asyncio
    async def test_a_node_outside_the_outline_gets_none(self):
        p = _plugin({"999": _section("999", f"Powers. {NOTE}")})
        result = await p.execute_tool("get_section", {"guid": "999"})
        assert result.success
        assert result.structured_content["title_attachments"] == []
        assert _searches(p) == []

    @pytest.mark.asyncio
    async def test_a_section_that_was_not_returned_has_the_field(self):
        p = _plugin({"c1705": _section("c1705", "Powers.")})
        result = await p.execute_tool(
            "get_sections", {"sections": ["c1705", "404404"]}
        )
        validate(result.structured_content, SECTIONS_OUTPUT_SCHEMA)
        assert result.structured_content["sections"][1][
            "title_attachments"] == []


class TestNotesOnOwnLines:
    def test_a_note_ending_at_the_attachment_sentence(self):
        out = EcodePlugin._notes_on_own_lines(f"{NOTE} {LAW}")
        assert out == f"{NOTE}\n{LAW}"

    def test_a_note_in_the_middle_of_a_sentence(self):
        text = (
            "related to the federal Family and Medical Leave Act, Editor's "
            "Note: See 5 U.S.C. § 6381.  the Alaska Family Leave "
            "Act, Editor's Note: See AS 23.40.205.  or any related local law."
        )
        assert EcodePlugin._notes_on_own_lines(text).splitlines() == [
            "related to the federal Family and Medical Leave Act,",
            "Editor's Note: See 5 U.S.C. § 6381.",
            "the Alaska Family Leave Act,",
            "Editor's Note: See AS 23.40.205.",
            "or any related local law.",
        ]

    def test_the_sentences_own_stop_stays_with_it(self):
        text = (
            "the area as depicted below Editor's Note: Exhibit A, Tax "
            "Incentive Areas, is included as an attachment to this title. "
            ". This includes properties downtown."
        )
        assert EcodePlugin._notes_on_own_lines(text).splitlines() == [
            "the area as depicted below.",
            "Editor's Note: Exhibit A, Tax Incentive Areas, is included as "
            "an attachment to this title.",
            "This includes properties downtown.",
        ]

    @pytest.mark.parametrize("text", [
        # No mark of where the note stops: law may follow after one space.
        "Editor’s note — Notwithstanding subsection 21.10.015A.3, "
        "this ordinance shall not require review. The following fees shall "
        "be paid:  Type Fee",
        # Runs to the end of its node.
        "Prohibitions. Editor's note — See Charter section 17.04.",
        # Inside the history note.
        "Text. (AO No. 97-87, § 1, 6-3-1997; Editor's note — "
        "AO No. 97-87 occasioned by Proposition 3.  AO No. 2001-51, "
        "§ 1, 2-27-2001)",
        "No notes here.  Two spaces prove nothing.",
    ])
    def test_a_note_whose_end_is_not_marked_is_left_alone(self, text):
        assert EcodePlugin._notes_on_own_lines(text) == text

    @pytest.mark.asyncio
    async def test_the_section_text_shows_the_note_apart(self):
        p = _plugin({"c1705": _section("c1705", f"Powers. {NOTE} {LAW}")})
        result = await p.execute_tool("get_section", {"guid": "c1705"})
        assert result.structured_content["text"] == f"{NOTE}\n{LAW}"

    @pytest.mark.asyncio
    async def test_history_is_still_parsed(self):
        content = f"Powers. {NOTE} {LAW} (AO No. 2013-93, prop. 8, 4-1-2014)"
        p = _plugin({"c1705": _section("c1705", content)})
        result = await p.execute_tool("get_section", {"guid": "c1705"})
        history = result.structured_content["history"]
        assert history["latest_date"] == "2014-04-01"
        assert "history: 1 entry in the history note" in result.content[0]["text"]
        assert "1 entries" not in result.content[0]["text"]


class TestDefinedTermPrintedOnce:
    def _defs(self, *children):
        return {"id": "s904010", "number": "§ 9.04.010",
                "title": "Definitions.", "type": "section",
                "content": "Definitions. In this chapter:",
                "children": list(children), "attachments": {}, "images": []}

    def _term(self, title, content, number=""):
        return {"id": "d", "number": number, "title": title,
                "content": content, "children": [], "type": "definition"}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("term", ['"Petition"', "“Petition”",
                                      '"Initiative,"'])
    async def test_a_quoted_term_heading_is_not_repeated(self, term):
        body = f"{term} means a written petition."
        p = _plugin({"s904010": self._defs(self._term(term, body))})
        result = await p.execute_tool("get_section", {"guid": "s904010"})
        assert result.structured_content["text"] == (
            f"In this chapter:\n  {body}"
        )

    @pytest.mark.asyncio
    async def test_a_numbered_term_keeps_its_number(self):
        p = _plugin({"s904010": self._defs(
            self._term('"Petition"', '"Petition" means a paper.', "1."))})
        result = await p.execute_tool("get_section", {"guid": "s904010"})
        assert '  1. "Petition" means a paper.' in result.structured_content["text"]
        assert result.structured_content["text"].count('"Petition"') == 1

    @pytest.mark.asyncio
    async def test_other_headings_are_still_printed(self):
        p = _plugin({"s904010": self._defs(
            # The term is the heading only: the text starts "means".
            self._term('"Petition"', "means a written petition."),
            # An ordinary heading that the text happens to start with.
            self._term("Fees", "Fees shall be paid."),
        )})
        result = await p.execute_tool("get_section", {"guid": "s904010"})
        assert result.structured_content["text"].splitlines()[1:] == [
            '  "Petition"', "  means a written petition.",
            "  Fees", "  Fees shall be paid.",
        ]


class TestCachedDocumentsFetchedTogether:
    @pytest.mark.asyncio
    async def test_two_documents_do_not_wait_for_each_other(self):
        p = EcodePlugin(CONFIG)
        p.plugin_config = EcodePluginConfig(**CONFIG)
        running = 0
        peak = 0
        fetched = []

        async def slow(path, params=None):
            nonlocal running, peak
            fetched.append(path)
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0.02)
            running -= 1
            return {"path": path}

        p._get_json = slow
        a, b, c = await asyncio.gather(
            p._get_static_json("/customer"),
            p._get_static_json("/toc"),
            p._get_static_json("/toc"),
        )
        assert peak == 2
        # ... while one document is still fetched once.
        assert sorted(fetched) == ["/customer", "/toc"]
        assert b is c and a != b
