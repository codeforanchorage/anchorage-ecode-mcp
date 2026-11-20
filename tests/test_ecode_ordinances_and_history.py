"""recent_ordinances and the parsed history on section reads (1.12.0).

eCode360 has no "what changed" endpoint (51 candidate paths probed on
2026-11-18; none exist). Two things in the code itself answer the question:

* § OL.010, the publisher's Ordinance List and Disposition Table, which the
  API returns as one run of text with single spaces between table cells;
* the history note that closes a section, "(AO No. 91-182, 12-17-1991; ...)".

The text below is the live AN6998 text, trimmed.
"""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from jsonschema import validate

from plugins.ecode.config_schema import EcodePluginConfig
from plugins.ecode.plugin import (
    CAVEAT_MORE_ORDINANCES,
    CAVEAT_ORDINANCE_LIST_SCOPE,
    CAVEAT_ORDINANCE_LIST_UNAVAILABLE,
    CAVEAT_ORDINANCE_ROWS_UNDATED,
    ORDINANCES_OUTPUT_SCHEMA,
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

OL_GUID = "50397147"

# Live rows, in table order (by ordinance number, not by date). Kept for
# what each one exercises: a doubled space before a cell, a slash date
# inside a subject, "(S)" and "Amended" suffixes, the Charter, a regulation
# chapter with and without "Ch.", a supplement number, an "(Eff. ...)" date
# inside a subject, "Chs." with commas inside a subject, and two rows that
# have no adoption date.
OL_TEXT = (
    "Ordinance List and Disposition Table Ord. No. Adoption Date Subject "
    "Disposition Supp. No. "
    "2025-8(S) Amended 2-11-2025 Amends Administration: Personnel Rules  "
    "Ch. 3.30 "
    "2025-138 12-16-2025 Amends Service Areas: Service Areas (Ballot "
    "Measure Passed 4/7/2026) Ch. 27.30 "
    "2026-15 1-27-2026 Amends Charter (Ballot Measure Passed 4/7/2026) CHA "
    "2026-16(S) 2-17-2026 Amends Transportation: General Provisions; "
    "Repeals Att. 1 Ch. 11.10; Ch. 14.60; Ch. R11.10 "
    "2026-27(S) Amended 3-24-2026 Amends Land Use Planning: Use "
    "Regulations; Chugiak-Eagle River (Also Amends Old Code Chs. 21.35, "
    "21.40, 21.50) Ch. 21.05; Ch. 21.10 "
    "2026-40(S-1) Amended 6-23-2026 Amends Purchasing: Methods of Source "
    "Selection (Eff. 9-1-2026) Ch. 7.10; Ch. 7.20 0.1 "
    "2026-46 Amended 4-28-2026 Amends Land Use Planning: Signs Ch. 21.07; "
    "Ch. 24.15; R21.13 "
    "2026-47 6-9-2026 Amends Land Use Planning: General Provisions "
    "Ch. 21.01 0.1 "
    "2026-51 Amends Land Use Planning: Girdwood Ch. 21.09 "
    "2026-63 Amends Administration: Personnel Rules Ch. 3.30 "
    "2026-118 10-20-2026 Amends Land Use Planning: General Provisions "
    "Ch. 21.01 0.4"
)

OL_NODE = {
    "id": OL_GUID,
    "number": "§\xa0OL.010",
    "title": "Ordinance List and Disposition Table",
    "type": "section",
    "content": OL_TEXT,
    "children": [],
    "attachments": {},
    "images": [],
}


def _toc(with_list=True):
    tables = [{
        "guid": "49672289", "label": "Title", "number": "CCT",
        "title": "Code Comparative Table", "children": [{
            "guid": "49672291", "label": "Section", "number": "§\xa0CCT.020",
            "title": "Ordinance Disposition Table.", "children": [],
        }],
    }]
    if with_list:
        # The Title and its one Section share a heading; the leaf is wanted.
        tables.append({
            "guid": "50397134", "label": "Title", "number": "OL",
            "title": "Ordinance List and Disposition Table", "children": [{
                "guid": OL_GUID, "label": "Section", "number": "§\xa0OL.010",
                "title": "Ordinance List and Disposition Table",
                "children": [],
            }],
        })
    return {"guid": "AN6998", "title": "Municipality of Anchorage, AK",
            "children": [{"guid": "49920376", "label": "Division",
                          "title": "TABLES", "children": tables}]}


def _plugin(content=None, toc=None):
    content = {OL_GUID: OL_NODE} if content is None else content
    toc = _toc() if toc is None else toc
    calls = []

    async def fake_get(path, params=None, **_kw):
        calls.append(path)
        if path.endswith("/code/toc"):
            return httpx.Response(200, json=toc, request=REQ)
        guid = path.rsplit("/", 1)[-1]
        if guid in content:
            return httpx.Response(200, json=content[guid], request=REQ)
        return httpx.Response(404, json={"message": "nope"}, request=REQ)

    p = EcodePlugin(CONFIG)
    p.plugin_config = EcodePluginConfig(**CONFIG)
    p.client = MagicMock()
    p.client.get = AsyncMock(side_effect=fake_get)
    p.calls = calls
    return p


async def _run(plugin, **args):
    result = await plugin.execute_tool("recent_ordinances", args)
    assert result.success, result.error_message
    sc = result.structured_content
    validate(sc, ORDINANCES_OUTPUT_SCHEMA)
    text = result.content[0]["text"]
    for caveat in sc["caveats"]:
        assert caveat["message"] in text
    return sc, text


def _numbers(sc):
    return [row["ordinance"] for row in sc["ordinances"]]


def _row(sc, number):
    return next(r for r in sc["ordinances"] if r["ordinance"] == number)


class TestOrdinanceRows:
    @pytest.mark.asyncio
    async def test_every_row_is_read_newest_first_undated_last(self):
        sc, _ = await _run(_plugin())
        assert _numbers(sc) == [
            "2026-118", "2026-40(S-1) Amended", "2026-47", "2026-46 Amended",
            "2026-27(S) Amended", "2026-16(S)", "2026-15", "2025-138",
            "2025-8(S) Amended", "2026-51", "2026-63",
        ]
        assert sc["total_count"] == 11
        assert sc["covers"] == {
            "count": 11,
            "earliest_adopted": "2025-02-11",
            "latest_adopted": "2026-10-20",
        }
        assert sc["source"]["guid"] == OL_GUID
        assert sc["source"]["url"] == f"https://ecode360.com/{OL_GUID}"

    @pytest.mark.asyncio
    async def test_cells_of_a_plain_row(self):
        sc, _ = await _run(_plugin())
        assert _row(sc, "2026-118") == {
            "ordinance": "2026-118",
            "adopted": "2026-10-20",
            "subject": "Amends Land Use Planning: General Provisions",
            "chapters": ["21.01"],
            "supplement": "0.4",
            "search_query": '"2026-118"',
            "text": "2026-118 10-20-2026 Amends Land Use Planning: General "
                    "Provisions Ch. 21.01 0.4",
        }

    @pytest.mark.asyncio
    async def test_a_chapter_number_is_not_taken_for_the_supplement(self):
        sc, _ = await _run(_plugin())
        row = _row(sc, "2025-8(S) Amended")
        assert row["chapters"] == ["3.30"] and row["supplement"] is None
        assert row["subject"] == "Amends Administration: Personnel Rules"

    @pytest.mark.asyncio
    async def test_dates_and_numbers_inside_a_subject_stay_in_it(self):
        sc, _ = await _run(_plugin())
        row = _row(sc, "2026-40(S-1) Amended")
        assert row["adopted"] == "2026-06-23"
        assert row["subject"].endswith("(Eff. 9-1-2026)")
        assert row["chapters"] == ["7.10", "7.20"]
        assert row["supplement"] == "0.1"
        old = _row(sc, "2026-27(S) Amended")
        assert "Chs. 21.35, 21.40, 21.50)" in old["subject"]
        assert old["chapters"] == ["21.05", "21.10"]
        assert "4/7/2026" in _row(sc, "2025-138")["subject"]

    @pytest.mark.asyncio
    async def test_charter_and_regulation_dispositions(self):
        sc, _ = await _run(_plugin())
        assert _row(sc, "2026-15")["chapters"] == ["Charter"]
        assert _row(sc, "2026-16(S)")["chapters"] == [
            "11.10", "14.60", "R11.10"
        ]
        assert _row(sc, "2026-46 Amended")["chapters"] == [
            "21.07", "24.15", "R21.13"
        ]

    @pytest.mark.asyncio
    async def test_the_search_query_drops_the_version_suffix(self):
        """The quoted number finds the sections whose note reads
        'AO No. 2026-27(S) Amended' -- and, unlike '"AO No. 2026-27"', the
        older notes that write 'AO 2013-117' with no 'No.' (verified live
        2026-11-20: 68 sections against 1)."""
        sc, text = await _run(_plugin())
        assert _row(sc, "2026-27(S) Amended")["search_query"] == '"2026-27"'
        assert 'search_code("2026-27")' in text

    @pytest.mark.asyncio
    async def test_rows_without_a_date_are_kept_and_flagged(self):
        sc, _ = await _run(_plugin())
        row = _row(sc, "2026-51")
        assert row["adopted"] is None and row["chapters"] == ["21.09"]
        codes = [c["code"] for c in sc["caveats"]]
        assert codes == [
            CAVEAT_ORDINANCE_LIST_SCOPE, CAVEAT_ORDINANCE_ROWS_UNDATED
        ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "mention", ["AO 2025-77 Amended", "AO No. 2025-77", "Ord. No. 2025-77"]
    )
    async def test_an_ordinance_named_inside_a_subject_is_not_a_row(
        self, mention
    ):
        node = dict(OL_NODE, content=(
            "Ord. No. Adoption Date Subject Disposition Supp. No. "
            f"2026-90 7-7-2026 Amends Taxation: Corrects {mention} "
            "Ch. 12.05 0.1"
        ))
        sc, _ = await _run(_plugin(content={OL_GUID: node}))
        assert _numbers(sc) == ["2026-90"]
        assert mention in sc["ordinances"][0]["subject"]

    @pytest.mark.asyncio
    async def test_the_first_row_follows_the_header_directly(self):
        """The header ends 'Supp. No.'; that 'No.' is not an ordinance
        mention, and the row after it was once dropped for it."""
        sc, _ = await _run(_plugin())
        assert "2025-8(S) Amended" in _numbers(sc)


class TestOrdinanceFilterAndLimit:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("within", ["Title 21", "21", "title 21", "21."])
    async def test_title(self, within):
        sc, text = await _run(_plugin(), within=within)
        assert _numbers(sc) == [
            "2026-118", "2026-47", "2026-46 Amended", "2026-27(S) Amended",
            "2026-51",
        ]
        assert sc["within"] == "21"
        assert "codified in Title 21" in sc["summary"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "within", ["Chapter 21.01", "Ch. 21.01", "21.01", "§ 21.01.080"]
    )
    async def test_chapter_and_a_section_narrowed_to_its_chapter(self, within):
        sc, _ = await _run(_plugin(), within=within)
        assert _numbers(sc) == ["2026-118", "2026-47"]
        assert "codified in Chapter 21.01" in sc["summary"]

    @pytest.mark.asyncio
    async def test_title_2_does_not_match_title_21_or_27(self):
        sc, _ = await _run(_plugin(), within="Title 2")
        assert sc["ordinances"] == [] and sc["total_count"] == 0
        assert sc["summary"].startswith("No recent ordinance(s)")
        # The table itself was read: the scope note still says what it holds.
        assert sc["covers"]["count"] == 11

    @pytest.mark.asyncio
    async def test_charter_and_regulations(self):
        sc, _ = await _run(_plugin(), within="Charter")
        assert _numbers(sc) == ["2026-15"]
        sc, _ = await _run(_plugin(), within="R21")
        assert _numbers(sc) == ["2026-46 Amended"]
        # Regulations are their own titles: Title 11 does not pull R11.10
        # in by itself, the ordinance is there for Ch. 11.10.
        sc, _ = await _run(_plugin(), within="R11.10")
        assert _numbers(sc) == ["2026-16(S)"]

    @pytest.mark.asyncio
    async def test_limit_keeps_the_newest_and_says_more_exist(self):
        sc, _ = await _run(_plugin(), limit=2)
        assert _numbers(sc) == ["2026-118", "2026-40(S-1) Amended"]
        assert sc["total_count"] == 11
        more = next(
            c for c in sc["caveats"] if c["code"] == CAVEAT_MORE_ORDINANCES
        )
        assert "9 older ordinance(s)" in more["message"]
        assert "limit=11" in more["message"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "args, said",
        [
            ({"within": "zoning"}, "is not a title or chapter"),
            ({"within": "x" * 300}, "within is too long"),
            ({"limit": 0}, "limit must be between 1 and 100"),
            ({"limit": 101}, "limit must be between 1 and 100"),
            ({"limit": "many"}, "limit must be an integer"),
        ],
    )
    async def test_bad_arguments_are_refused_before_any_request(
        self, args, said
    ):
        p = _plugin()
        result = await p.execute_tool("recent_ordinances", args)
        assert result.success is False
        assert said in result.error_message
        assert p.calls == []


class TestOrdinanceListMissing:
    @pytest.mark.asyncio
    async def test_a_code_without_the_table_says_so(self):
        p = _plugin(toc=_toc(with_list=False))
        sc, text = await _run(p)
        assert sc["ordinances"] == [] and sc["source"] is None
        assert [c["code"] for c in sc["caveats"]] == [
            CAVEAT_ORDINANCE_LIST_UNAVAILABLE
        ]
        assert "not evidence that nothing changed" in text
        # No section was fetched: no guid was guessed. (The search for
        # ordinances held as PDFs, added in 1.16.0, is not a section read.)
        assert not any("/code/content/" in path for path in p.calls)

    @pytest.mark.asyncio
    async def test_a_table_that_no_longer_parses_says_so(self):
        node = dict(OL_NODE, content="See the attached table of ordinances.")
        sc, _ = await _run(_plugin(content={OL_GUID: node}))
        assert sc["ordinances"] == []
        assert "layout may have changed" in sc["caveats"][0]["message"]

    def test_tool_is_registered_read_only_with_its_schema(self):
        p = EcodePlugin(CONFIG)
        p.plugin_config = EcodePluginConfig(**CONFIG)
        tool = next(t for t in p.get_tools() if t.name == "recent_ordinances")
        assert tool.output_schema == ORDINANCES_OUTPUT_SCHEMA
        assert tool.annotations.get("readOnlyHint") is True
        assert tool.input_schema["required"] == []


# ── Section history ────────────────────────────────────────────────────

DUI_NOTE = (
    "(AO No. 267-76; AO No. 78-72; AO No. 2001-159, §§\xa01, 2, 9-11-2001; "
    "AO No. 2011-113(S), §\xa059, 11-22-2011, eff. 12-22-2011; "
    "AO No. 2011-61(S), §\xa01, 7-12-2011, retro eff. 7-4-2011; "
    "AO No. 2018-43(S), §§\xa01(Exh. B; part 2), 2, 6-12-2018; "
    "AO No. 2026-19 Amended, 2-17-2026; "
    "AO No. 2024-77, §\xa01, 9-10-2024, eff. 4-1-2025)"
)


def _section(guid, content, children=()):
    return {
        "id": guid, "number": "§\xa09.28.020", "title": "DUI.",
        "type": "section", "content": content, "children": list(children),
        "attachments": {}, "images": [],
    }


async def _history(content, children=()):
    p = _plugin(content={"900": _section("900", content, children)})
    result = await p.execute_tool("get_section", {"guid": "900"})
    assert result.success, result.error_message
    validate(result.structured_content, SECTION_OUTPUT_SCHEMA)
    return result.structured_content["history"], result.content[0]["text"]


class TestSectionHistory:
    @pytest.mark.asyncio
    async def test_entries_are_parsed_in_printed_order(self):
        history, text = await _history(f"DUI. A person commits it. {DUI_NOTE}")
        assert [e["citation"] for e in history["entries"]] == [
            "AO No. 267-76", "AO No. 78-72", "AO No. 2001-159",
            "AO No. 2011-113(S)", "AO No. 2011-61(S)", "AO No. 2018-43(S)",
            "AO No. 2026-19 Amended", "AO No. 2024-77",
        ]
        assert [e["date"] for e in history["entries"]] == [
            None, None, "2001-09-11", "2011-11-22", "2011-07-12",
            "2018-06-12", "2026-02-17", "2024-09-10",
        ]
        assert [e["effective_date"] for e in history["entries"]] == [
            None, None, None, "2011-12-22", "2011-07-04", None, None,
            "2025-04-01",
        ]
        # The entry as printed is kept, including the ordinance's own §
        # (whitespace normalised: the API's no-break spaces become spaces).
        assert history["entries"][3]["text"] == (
            "AO No. 2011-113(S), § 59, 11-22-2011, eff. 12-22-2011"
        )

    @pytest.mark.asyncio
    async def test_latest_is_by_date_not_by_position(self):
        history, text = await _history(f"DUI. Text. {DUI_NOTE}")
        assert history["latest_date"] == "2026-02-17"
        assert history["latest_citation"] == "AO No. 2026-19 Amended"
        assert (
            "history: 8 entries in the history note; the newest dated one "
            "is AO No. 2026-19 Amended, 2026-02-17" in text
        )

    @pytest.mark.asyncio
    async def test_a_semicolon_inside_parentheses_does_not_split(self):
        history, _ = await _history(f"DUI. Text. {DUI_NOTE}")
        assert history["entries"][5]["text"] == (
            "AO No. 2018-43(S), §§ 1(Exh. B; part 2), 2, 6-12-2018"
        )

    @pytest.mark.asyncio
    async def test_a_section_with_subsections(self):
        """With children the note still closes the parent's own text."""
        kids = [{"id": "901", "number": "A.", "title": "DUI.",
                 "content": "A person shall not drive.", "children": []}]
        history, _ = await _history(
            "DUI. (AO No. 91-182, 12-17-1991; AO No. 2005-90, §\xa02, "
            "1-1-2005)", kids
        )
        assert history["latest_date"] == "2005-01-01"
        assert len(history["entries"]) == 2

    @pytest.mark.asyncio
    async def test_undated_only_note_has_entries_but_no_latest(self):
        history, text = await _history("Signs. (CAC 10.20.010; AO No. 83-49)")
        assert [e["citation"] for e in history["entries"]] == [
            "CAC 10.20.010", "AO No. 83-49"
        ]
        assert history["latest_date"] is None
        assert history["latest_citation"] is None
        assert "history:" not in text

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "content",
        [
            "through §\xa019.20.170. (Reserved)",
            "Prior organizations continued.",
            "A fee applies (see section 2.30.010).",
            "",
        ],
    )
    async def test_no_history_note_is_null(self, content):
        history, text = await _history(content)
        assert history is None
        assert "history:" not in text

    @pytest.mark.asyncio
    async def test_an_impossible_date_is_not_reported_as_one(self):
        history, _ = await _history("X. Text. (AO No. 2020-1, 13-45-2020)")
        assert history["entries"][0]["date"] is None
        assert history["entries"][0]["text"] == "AO No. 2020-1, 13-45-2020"

    @pytest.mark.asyncio
    async def test_batch_items_carry_history_too(self):
        p = _plugin(content={
            "900": _section("900", "DUI. Text. (AO No. 2024-77, 9-10-2024)"),
            "901": _section("901", "Other. Text."),
        })
        result = await p.execute_tool(
            "get_sections", {"sections": ["900", "901"]}
        )
        sc = result.structured_content
        validate(sc, SECTIONS_OUTPUT_SCHEMA)
        assert sc["sections"][0]["history"]["latest_date"] == "2024-09-10"
        assert sc["sections"][1]["history"] is None
