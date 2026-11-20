"""find_ordinance: one ordinance's rows in the Ordinance Disposition Table
(1.13.0).

§ CCT.020 maps every ordinance since the 1977 code to the sections it
changed. The API returns the four-column table as ONE run of cells
separated by single spaces, where an empty cell is a lone no-break space.
The rows below are copied from the live table (2026-11-19) except the two
marked as made up, which reuse live cell shapes under invented numbers.
"""

import httpx
import pytest
from jsonschema import validate
from unittest.mock import AsyncMock, MagicMock

import plugins.ecode.plugin as ecode_mod
from plugins.ecode.config_schema import EcodePluginConfig
from plugins.ecode.plugin import (
    CAVEAT_ORDINANCE_ENTRIES_TRUNCATED,
    CAVEAT_ORDINANCE_NOT_IN_TABLE,
    CAVEAT_ORDINANCE_SEVERAL_VERSIONS,
    CAVEAT_ORDINANCE_TABLE_SCOPE,
    CAVEAT_ORDINANCE_TABLE_UNAVAILABLE,
    FIND_ORDINANCE_OUTPUT_SCHEMA,
    EcodePlugin,
)
from tests.test_ecode_ordinances_and_history import OL_GUID, OL_NODE, _toc

CONFIG = {
    "enabled": True,
    "base_url": "https://api.ecode360.com",
    "customer_id": "AN6998",
    "city_name": "Municipality of Anchorage",
    "timeout": 20,
}
REQ = httpx.Request("GET", "https://api.ecode360.com/v1/x")
CCT_GUID = "49672291"
E = "\xa0"  # an empty cell
S = "§\xa0"  # the section sign and the no-break space after it

CCT_TEXT = (
    "Ordinance Disposition Table. This table gives the location within the "
    "Code, the Regulations, or the Charter of those ordinances adopted "
    "since the adoption of the Anchorage Municipal Code in 1977. Ordinances "
    "adopted since August 30, 1977, and not listed herein have been omitted "
    "as repealed, superseded or not of a general and permanent nature. "
    "Ordinance Number Section Effective Date Disposition "
    # No ordinance sections or dates before 2000.
    f"13-75 {E} {E} {S}2.30.010 {E} {E} {E} {S}2.30.020 "
    f"85-8 {E} {E} {S}R7.40.008 {E} {E} {E} {S}R16.02.001—{S}R16.02.005 "
    # "11-17" after a comma is part of the disposition, not an ordinance.
    f"2000-71(S-3) 1 6-27-2000 {S}21.45.263 {E} 2 {E} "
    f"{S}21.45.265A.3.d, 4.c, 11-17 {E} 3 {E} {S}21.50.280D "
    # Listed in two places; "Added" with no date cell before it.
    f"2000-107 1 7-25-2000 {S}11.20.080F.3 {E} 3 Added {S}11.30.150 "
    f"2000-107 4 {E} {S}R11.10.004A.21, 22 "
    # The section cell typed onto the number.
    f"2000-127(S)1 {E} 10-14-2000 {S}11.70.020—{S}11.70.050 "
    # A mistyped date ("10 7-03"): "7-03" must not start an ordinance.
    f"2003-132 1 10 7-03 {S}21.35.020 {E} 2 {E} {S}21.40.020B.5 "
    # Made up: one number under two printed forms.
    f"2008-134 1 1-1-2009 {S}3.30.010 2008-134(S) 1 2-1-2009 {S}3.30.020 "
    # A later ordinance amending this one, listed under its number.
    f"2014-137(S) 1 11-18-2014 {S}9.54.005—{S}9.54.010 {E} {E} Added "
    f"{S}9.54.015 {E} 3 {E} {S}14.60.030 Amd. of 2014-137(S) 2 12-16-2014 "
    f"{S}10.54.040A.4.d "
    # Several dates in one cell; a later row with its own date.
    f"2016-53 1 7-4-2016, 9-12-2016, 1-1-2017 {S}3.30.022 {E} 2 1-1-2017 "
    f"Rpld {S}3.30.125H "
    # Made-up number; the renumbering rows are live: one change, two rows.
    f"2019-1 1 1-1-2019 {S}3.30.172F.7 {E} {E} Rnbd {S}3.30.172.F.7—21 "
    f"{E} {E} as {S}3.30.172.F.8—22 "
    f"2020-126 1 (Prop. 9) 12-8-2020/election of 4-6-2021 {S}27.30.700 "
    f"2025-115(S-2) 1 5-1-2026 Added {S}10.90.010, {S}10.90.020 {E} 2 {E} "
    f"{S}12.20.031G {E} {E} Added {S}12.20.031K {E} 4 (Exh. A) {E} "
    f"{S}21.05.010E, Table 21.05-1 "
    # Also in the Ordinance List fixture (adopted 6-9-2026).
    f"2026-47 1 6-9-2026 {S}21.01.030"
)

CCT_NODE = {
    "id": CCT_GUID,
    "number": "§\xa0CCT.020",
    "title": "Ordinance Disposition Table.",
    "type": "section",
    "content": CCT_TEXT,
    "children": [],
    "attachments": {},
    "images": [],
}


def _plugin(content=None, toc=None):
    if content is None:
        content = {OL_GUID: OL_NODE, CCT_GUID: CCT_NODE}
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


async def _find(ordinance, plugin=None):
    plugin = plugin or _plugin()
    result = await plugin.execute_tool("find_ordinance", {"ordinance": ordinance})
    assert result.success, result.error_message
    sc = result.structured_content
    validate(sc, FIND_ORDINANCE_OUTPUT_SCHEMA)
    text = result.content[0]["text"]
    for caveat in sc["caveats"]:
        assert caveat["message"] in text
    return sc, text


def _codes(sc):
    return [c["code"] for c in sc["caveats"]]


class TestLookup:
    @pytest.mark.asyncio
    async def test_sections_actions_and_effective_date(self):
        sc, text = await _find("AO No. 2025-115(S-2)")

        assert sc["found"] is True
        assert sc["ordinance"] == "2025-115(S-2)"
        (match,) = sc["matches"]
        assert match["ordinance"] == "2025-115(S-2)"
        assert match["effective_date"] == "2026-05-01"
        assert match["entry_count"] == sc["total_entries"] == 4
        assert [
            (e["ordinance_section"], e["action"], e["disposition"])
            for e in match["entries"]
        ] == [
            ("1", "added", "§ 10.90.010, § 10.90.020"),
            ("2", None, "§ 12.20.031G"),
            (None, "added", "§ 12.20.031K"),
            ("4 (Exh. A)", None, "§ 21.05.010E, Table 21.05-1"),
        ]
        assert match["sections"] == [
            "10.90.010", "10.90.020", "12.20.031", "21.05.010",
        ]
        # Only the first row prints the date.
        assert [e["effective_date"] for e in match["entries"]] == [
            "2026-05-01", None, None, None,
        ]
        assert _codes(sc) == [CAVEAT_ORDINANCE_TABLE_SCOPE]
        assert "ord. § 4 (Exh. A): § 21.05.010E" in text
        assert "EFFECTIVE dates" in text
        assert sc["source"] == {
            "guid": CCT_GUID,
            "ref": "§\xa0CCT.020",
            "title": "Ordinance Disposition Table.",
            "url": f"https://ecode360.com/{CCT_GUID}",
        }
        assert sc["search_query"] == '"2025-115"'

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "asked",
        [
            "2025-115",
            "AO 2025-115",
            "ao no. 2025-115",
            "Ordinance 2025-115(S-2), As Amended",
            "2025 - 115",
            "2025–115",
            "AO 2025-115 (S-2)",
        ],
    )
    async def test_number_is_read_out_of_how_it_is_written(self, asked):
        sc, _ = await _find(asked)
        assert sc["found"] is True
        assert sc["matches"][0]["ordinance"] == "2025-115(S-2)"

    @pytest.mark.asyncio
    async def test_old_ordinance_has_no_sections_or_dates(self):
        sc, text = await _find("85-8")
        (match,) = sc["matches"]

        assert match["effective_date"] is None
        assert match["effective_as_printed"] is None
        assert [e["ordinance_section"] for e in match["entries"]] == [None, None]
        assert match["sections"] == ["R7.40.008", "R16.02.001", "R16.02.005"]
        assert "  -: § R7.40.008" in text

    @pytest.mark.asyncio
    async def test_table_and_list_are_each_fetched_once(self):
        p = _plugin()
        await _find("2025-115", p)
        await _find("2016-53", p)

        assert sum(1 for c in p.calls if c.endswith(CCT_GUID)) == 1
        assert sum(1 for c in p.calls if c.endswith("/code/toc")) == 1


class TestHandTypedTable:
    """Each is a shape found in the live table that a tidier parser
    would misread."""

    def _blocks(self):
        return _plugin()._parse_ordinance_table(CCT_TEXT)

    def test_every_ordinance_and_no_others(self):
        assert [b["ordinance"] for b in self._blocks()] == [
            "13-75", "85-8", "2000-71(S-3)", "2000-107", "2000-107",
            "2000-127(S)", "2003-132", "2008-134", "2008-134(S)",
            "2014-137(S)", "Amd. of 2014-137(S)", "2016-53", "2019-1",
            "2020-126", "2025-115(S-2)", "2026-47",
        ]

    def test_every_section_sign_lands_in_a_disposition(self):
        body = CCT_TEXT[CCT_TEXT.index("Disposition ") :]
        parsed = sum(
            e["disposition"].count("§")
            for b in self._blocks()
            for e in b["entries"]
        )
        assert parsed == body.count("§")

    @pytest.mark.asyncio
    async def test_number_after_a_comma_stays_in_the_disposition(self):
        sc, _ = await _find("2000-71")
        entries = sc["matches"][0]["entries"]

        assert [e["ordinance_section"] for e in entries] == ["1", "2", "3"]
        assert entries[1]["disposition"] == "§ 21.45.265A.3.d, 4.c, 11-17"
        assert (await _find("11-17"))[0]["found"] is False

    @pytest.mark.asyncio
    async def test_mistyped_date_does_not_start_an_ordinance(self):
        sc, _ = await _find("2003-132")
        entries = sc["matches"][0]["entries"]

        assert [e["disposition"] for e in entries] == [
            "§ 21.35.020", "§ 21.40.020B.5",
        ]
        assert (await _find("7-03"))[0]["found"] is False

    @pytest.mark.asyncio
    async def test_number_listed_in_two_places_is_one_match(self):
        sc, _ = await _find("2000-107")
        (match,) = sc["matches"]

        assert [e["ordinance_section"] for e in match["entries"]] == [
            "1", "3", "4",
        ]
        assert match["entries"][1]["action"] == "added"
        assert match["entries"][1]["effective_date"] is None
        assert CAVEAT_ORDINANCE_SEVERAL_VERSIONS not in _codes(sc)

    @pytest.mark.asyncio
    async def test_section_typed_onto_the_number(self):
        sc, _ = await _find("2000-127")
        (match,) = sc["matches"]

        assert match["ordinance"] == "2000-127(S)"
        assert match["effective_date"] == "2000-10-14"
        assert match["entries"][0]["ordinance_section"] == "1"
        assert match["sections"] == ["11.70.020", "11.70.050"]

    @pytest.mark.asyncio
    async def test_versions_are_separate_matches(self):
        sc, text = await _find("2008-134")

        assert [m["ordinance"] for m in sc["matches"]] == [
            "2008-134", "2008-134(S)",
        ]
        assert sc["total_entries"] == 2
        assert CAVEAT_ORDINANCE_SEVERAL_VERSIONS in _codes(sc)
        assert "under 2 forms of the number" in text

    @pytest.mark.asyncio
    async def test_amending_ordinance_row_is_kept_apart(self):
        sc, text = await _find("2014-137")
        first, later = sc["matches"]

        assert later["ordinance"] == "Amd. of 2014-137(S)"
        assert later["effective_date"] == "2014-12-16"
        # "Amd. of" must not trail the row before it.
        assert first["entries"][-1]["disposition"] == "§ 14.60.030"
        assert "\nAmd. of 2014-137(S) -- effective 2014-12-16" in text

    @pytest.mark.asyncio
    async def test_several_dates_are_kept_as_printed(self):
        sc, text = await _find("2016-53")
        (match,) = sc["matches"]

        assert match["effective_date"] is None
        assert match["effective_as_printed"] == "7-4-2016, 9-12-2016, 1-1-2017"
        second = match["entries"][1]
        assert (second["action"], second["effective_date"]) == (
            "repealed", "2017-01-01",
        )
        assert "repealed § 3.30.125H  [effective 2017-01-01]" in text

    @pytest.mark.asyncio
    async def test_election_date_and_proposition(self):
        sc, _ = await _find("2020-126")
        (entry,) = sc["matches"][0]["entries"]

        assert entry["ordinance_section"] == "1 (Prop. 9)"
        assert entry["effective_as_printed"] == "12-8-2020/election of 4-6-2021"
        assert entry["effective_date"] is None
        assert entry["disposition"] == "§ 27.30.700"

    @pytest.mark.asyncio
    async def test_renumbering_printed_on_two_rows_is_one_entry(self):
        sc, _ = await _find("2019-1")
        entries = sc["matches"][0]["entries"]

        assert len(entries) == 2
        assert entries[1]["action"] == "renumbered"
        assert entries[1]["disposition"] == (
            "§ 3.30.172.F.7—21 as § 3.30.172.F.8—22"
        )


class TestNotListed:
    @pytest.mark.asyncio
    async def test_unlisted_number_is_not_an_error(self):
        sc, text = await _find("2031-5")

        assert sc["found"] is False
        assert sc["matches"] == [] and sc["total_entries"] == 0
        assert sc["listing"] is None
        assert _codes(sc) == [CAVEAT_ORDINANCE_NOT_IN_TABLE]
        assert sc["covers"] == {
            "count": 15,
            "first_ordinance": "13-75",
            "last_ordinance": "2026-47",
        }
        assert "does not show the ordinance does not exist" in text
        assert '"2031-5"' in text

    @pytest.mark.asyncio
    async def test_recent_ordinance_comes_from_the_list(self):
        """2026-40 is in the Ordinance List but too new for the table."""
        sc, text = await _find("AO 2026-40")

        assert sc["found"] is False
        assert sc["listing"]["ordinance"] == "2026-40(S-1) Amended"
        assert sc["listing"]["adopted"] == "2026-06-23"
        assert sc["listing"]["chapters"] == ["7.10", "7.20"]
        assert _codes(sc) == [CAVEAT_ORDINANCE_NOT_IN_TABLE]
        assert "probably too new for this table" in text
        assert "Adopted 2026-06-23" in text

    @pytest.mark.asyncio
    async def test_listed_in_both_gives_subject_and_sections(self):
        sc, text = await _find("2026-47")

        assert sc["found"] is True
        assert sc["matches"][0]["sections"] == ["21.01.030"]
        assert sc["listing"]["adopted"] == "2026-06-09"
        assert sc["listing"]["subject"].startswith("Amends Land Use Planning")
        assert "Codified in: 21.01" in text

    @pytest.mark.asyncio
    async def test_code_without_the_table(self):
        toc = {"guid": "AN6998", "title": "X", "children": []}
        sc, text = await _find("2025-115", _plugin(toc=toc))

        assert sc["found"] is False
        assert sc["source"] is None and sc["covers"] is None
        assert _codes(sc) == [CAVEAT_ORDINANCE_TABLE_UNAVAILABLE]
        assert "no 'Ordinance Disposition Table' section" in text

    @pytest.mark.asyncio
    async def test_table_whose_layout_changed(self):
        node = {**CCT_NODE, "content": "Ordinance Disposition Table. Moved."}
        sc, text = await _find(
            "2025-115", _plugin(content={OL_GUID: OL_NODE, CCT_GUID: node})
        )

        assert sc["found"] is False
        assert _codes(sc) == [CAVEAT_ORDINANCE_TABLE_UNAVAILABLE]
        assert "layout may have changed" in text

    @pytest.mark.asyncio
    async def test_long_ordinance_is_cut_and_says_so(self, monkeypatch):
        monkeypatch.setattr(ecode_mod, "MAX_ORDINANCE_ENTRIES", 2)
        sc, text = await _find("2025-115")
        (match,) = sc["matches"]

        assert len(match["entries"]) == 2
        assert match["entry_count"] == sc["total_entries"] == 4
        # The section list still covers every row.
        assert "21.05.010" in match["sections"]
        assert CAVEAT_ORDINANCE_ENTRIES_TRUNCATED in _codes(sc)
        assert "INCOMPLETE: 2 of 4 entries" in text


class TestArgument:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "args, expected",
        [
            ({}, "ordinance is required"),
            ({"ordinance": None}, "ordinance is required"),
            ({"ordinance": "  "}, "ordinance is required"),
            ({"ordinance": ["2025-94"]}, "ordinance is required"),
            ({"ordinance": "zoning"}, "is not an ordinance number"),
            ({"ordinance": "2025"}, "is not an ordinance number"),
            ({"ordinance": "6-25-2024"}, "is not an ordinance number"),
            ({"ordinance": "2025-94 and 2025-95"}, "more than one ordinance"),
            ({"ordinance": "2025-94 " + "x" * 100}, "too long"),
        ],
    )
    async def test_refused_before_any_request(self, args, expected):
        p = _plugin()
        result = await p.execute_tool("find_ordinance", args)

        assert result.success is False
        assert expected in result.error_message
        assert p.calls == []

    def test_tool_is_declared(self):
        p = EcodePlugin(CONFIG)
        p.plugin_config = EcodePluginConfig(**CONFIG)
        tool = next(t for t in p.get_tools() if t.name == "find_ordinance")

        assert tool.input_schema["required"] == ["ordinance"]
        assert tool.output_schema == FIND_ORDINANCE_OUTPUT_SCHEMA
        assert "effective date" in tool.description


# ── 1.13.1: found in the pre-rollout scan of 2026-11-19 ──────────────────


def _toc_with_sections(*numbers):
    toc = _toc()
    toc["children"].append({
        "guid": "1", "label": "Title", "number": "21", "title": "T",
        "children": [
            {"guid": str(100 + i), "label": "Section",
             "number": f"§\xa0{n}", "title": "S", "children": []}
            for i, n in enumerate(numbers)
        ],
    })
    return toc


class TestSectionsAgainstTheOutline:
    """A disposition names a provision; the section it belongs to is
    whatever the outline says is a section, not the first three parts."""

    NUMBERS = {
        "21.05.070", "23.10.104.15", "3.30.121", "3.30.172", "R21.20.002",
        "9.54.005", "23.85.R317.3",
    }

    @pytest.mark.parametrize(
        "disposition, current, gone",
        [
            ("§ 21.05.070D.13b.vii(C)", ["21.05.070"], []),
            # Title 23 numbers run past three parts.
            ("§ 23.10.104.15.1A.1", ["23.10.104.15"], []),
            ("§ 23.85.R317.3.1", ["23.85.R317.3"], []),
            ("§ R21.20.002A.12", ["R21.20.002"], []),
            ("§ 3.30.172.F.7—21 as § 3.30.172.F.8—22",
             ["3.30.172"], []),
            # 3.30.121 is a section; 3.30.1213 is a different number.
            ("§ 3.30.1213", [], ["3.30.1213"]),
            # Mistyped in the table; repealed since.
            ("§ 21.050.030B.4.a", [], ["21.050.030"]),
            ("§ 21.45.110", [], ["21.45.110"]),
            ("§ 9.54.005—§ 9.54.010", ["9.54.005"], ["9.54.010"]),
            ("Ch. 21.04, (note)", [], []),
            ("Old 21.35.020B", [], []),
        ],
    )
    def test_resolution(self, disposition, current, gone):
        assert EcodePlugin._ordinance_code_sections(
            disposition, self.NUMBERS
        ) == (current, gone)

    def test_chapters_and_titles_are_not_sections(self):
        toc = {"children": [
            {"number": "21", "children": [
                {"number": "Ch 21.05", "children": [
                    {"number": "§\xa021.05.070"}]}]}]}
        assert EcodePlugin._outline_section_numbers(toc) == {"21.05.070"}

    @pytest.mark.asyncio
    async def test_sections_no_longer_in_the_code_are_listed_apart(self):
        toc = _toc_with_sections("10.90.010", "10.90.020", "21.05.010")
        sc, text = await _find("2025-115", _plugin(toc=toc))
        (match,) = sc["matches"]

        assert match["sections"] == ["10.90.010", "10.90.020", "21.05.010"]
        assert match["sections_not_in_code"] == ["12.20.031"]
        assert match["entries"][1]["sections_not_in_code"] == ["12.20.031"]
        assert "not sections of the code today (1): 12.20.031" in text
        assert "get_sections will not find it" in text


class TestNumberAsWritten:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("asked", ["2025-0115", "AO 2025-0115(S-2)"])
    async def test_padded_number(self, asked):
        sc, _ = await _find(asked)
        assert sc["found"] is True
        assert sc["matches"][0]["ordinance"] == "2025-115(S-2)"

    @pytest.mark.asyncio
    async def test_four_digit_year_before_2000(self):
        sc, _ = await _find("AO 1985-8")

        assert sc["found"] is True
        assert sc["matches"][0]["ordinance"] == "85-8"
        assert sc["search_query"] == '"85-8"'

    @pytest.mark.asyncio
    async def test_unlisted_number_explains_the_numbering(self):
        _, text = await _find("1991-7")
        assert "'85-8' is no. 8 of 1985" in text
        assert "'13-75'" in text

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "asked",
        [
            "AR 2025-94", "AR No. 2025-94", "Resolution 2025-94",
            "ar 2025-94", "AR2025-94", "A.R. 2025-94",
        ],
    )
    async def test_resolution_is_not_answered_as_the_ordinance(self, asked):
        """AR 2025-94 and AO 2025-94 are different acts with one number."""
        p = _plugin()
        result = await p.execute_tool("find_ordinance", {"ordinance": asked})

        assert result.success is False
        assert "Assembly resolution" in result.error_message
        assert "'AO 2025-94'" in result.error_message
        assert p.calls == []


class TestTableIsKeptLonger:
    @pytest.mark.asyncio
    async def test_table_outlives_the_five_minute_cache(self):
        """eCode360 takes about 9 s to return the table."""
        p = _plugin()
        await _find("2025-115", p)
        ecode_mod._RECENT_CACHE.clear()
        await _find("2016-53", p)

        assert sum(1 for c in p.calls if c.endswith(CCT_GUID)) == 1


# ── 1.16.0: ordinances the publisher holds as PDFs ───────────────────────

from plugins.ecode.plugin import (  # noqa: E402
    CAVEAT_ORDINANCE_PDF_AVAILABLE,
    CAVEAT_ORDINANCES_NOT_YET_CODIFIED,
    ORDINANCES_OUTPUT_SCHEMA,
    PENDING_ORDINANCE_QUERY,
)

PDF_108 = "https://live.ecode360.com/AN6998/laws/LF2907762.pdf"
PDF_98 = "https://live.ecode360.com/AN6998/laws/LF2907761.pdf"
# As the live search returned them on 2026-11-19, plus hits that must be
# ignored: another city's PDF, a code section, a table attachment.
LAW_HITS = [
    {"id": "LF2907762", "type": "law", "pdf": True, "url": PDF_108,
     "title": 'AO No. <em class="highlight">2026-108</em>(S)', "text": "x"},
    {"id": "LF2907761", "type": "law", "pdf": True, "url": PDF_98 + "?h=1",
     "title": "AO No. 2026-98(S-1)", "text": "mentions AO 2026-47"},
    {"id": "LF1", "type": "law", "pdf": True, "title": "AO No. 2026-1",
     "url": "https://live.ecode360.com/VA4680/laws/LF1.pdf", "text": "x"},
    {"id": "49672102", "type": "code", "pdf": False, "title": "AO No. 1",
     "url": "https://ecode360.com/49672102", "text": "x"},
    {"id": "7", "type": "doc", "pdf": True, "title": "AO No. table",
     "url": "https://live.ecode360.com/documents/AN6998/t.pdf", "text": "x"},
]


def _plugin_with_pdfs(hits=LAW_HITS, search_status=200, **kwargs):
    p = _plugin(**kwargs)
    inner = p.client.get.side_effect
    queries = []

    async def fake_get(path, params=None, **kw):
        if path.endswith("/search"):
            p.calls.append(path)
            queries.append(dict(params or {}))
            if search_status != 200:
                return httpx.Response(search_status, json={}, request=REQ)
            return httpx.Response(200, request=REQ, json={
                "results": hits if int(params["page"]) == 0 else [],
                "page": str(params["page"]), "maxPages": 1,
                "totalResultCount": len(hits)})
        return await inner(path, params=params, **kw)

    p.client.get = AsyncMock(side_effect=fake_get)
    p.queries = queries
    return p


class TestOrdinanceItselfAsPdf:
    """Launch day: a user looked up AO 2026-108(S), was told 'not listed',
    searched for it, and only then found the PDF. Five of nine lookups that
    day were for ordinances too new for the table."""

    @pytest.mark.asyncio
    async def test_not_in_the_table_but_held_as_a_pdf(self):
        p = _plugin_with_pdfs()
        sc, text = await _find("AO 2026-108(S)", p)

        assert sc["found"] is False
        assert sc["ordinance_pdfs"] == [
            {"ordinance": "AO No. 2026-108(S)", "url": PDF_108,
             "subject": None, "amends": []}
        ]
        assert _codes(sc) == [
            CAVEAT_ORDINANCE_NOT_IN_TABLE, CAVEAT_ORDINANCE_PDF_AVAILABLE,
        ]
        assert sc["summary"].startswith("AO No. 2026-108(S) has been adopted")
        assert text.startswith(sc["summary"])
        assert PDF_108 in text and "read_attachment" in text
        # One search for the ordinance PDFs, one for sections citing it.
        assert p.queries == [
            {"query": PENDING_ORDINANCE_QUERY, "page": 0},
            {"query": '"2026-108"', "page": 0},
        ]
        assert sc["citing_sections"] == []

    @pytest.mark.asyncio
    async def test_matched_on_the_title_not_on_a_mention(self):
        """AO 2026-98's text mentions 2026-47; that is not 2026-47's PDF.
        And 2026-10 is not 2026-108."""
        for asked in ("2026-47", "2026-10"):
            sc, _ = await _find(asked, _plugin_with_pdfs())
            assert sc["ordinance_pdfs"] == []
            assert CAVEAT_ORDINANCE_PDF_AVAILABLE not in _codes(sc)

    @pytest.mark.asyncio
    async def test_link_is_one_read_attachment_accepts(self):
        sc, _ = await _find("2026-98", _plugin_with_pdfs())
        (pdf,) = sc["ordinance_pdfs"]

        assert (pdf["ordinance"], pdf["url"]) == ("AO No. 2026-98(S-1)", PDF_98)
        p = _plugin()
        assert p._attachment_url_arg({"url": pdf["url"]}) == PDF_98

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [500, 404])
    async def test_a_failed_check_never_fails_the_lookup(self, status):
        sc, _ = await _find("2025-115", _plugin_with_pdfs(search_status=status))

        assert sc["found"] is True
        assert sc["ordinance_pdfs"] == []

    @pytest.mark.asyncio
    async def test_no_pdfs_at_all(self):
        sc, _ = await _find("2031-5", _plugin_with_pdfs(hits=[]))
        assert sc["ordinance_pdfs"] == []
        assert _codes(sc) == [CAVEAT_ORDINANCE_NOT_IN_TABLE]


class TestRecentOrdinancesNotYetCodified:
    async def _recent(self, plugin, **args):
        result = await plugin.execute_tool("recent_ordinances", args)
        assert result.success, result.error_message
        sc = result.structured_content
        validate(sc, ORDINANCES_OUTPUT_SCHEMA)
        text = result.content[0]["text"]
        for caveat in sc["caveats"]:
            assert caveat["message"] in text
        return sc, text

    @pytest.mark.asyncio
    async def test_lists_what_is_newer_than_the_list(self):
        sc, text = await self._recent(_plugin_with_pdfs())

        assert [(o["ordinance"], o["url"]) for o in sc["not_yet_codified"]] == [
            ("AO No. 2026-108(S)", PDF_108),
            ("AO No. 2026-98(S-1)", PDF_98),
        ]
        # Titles are not read in this test (see conftest): reported as such.
        assert all(o["subject"] is None for o in sc["not_yet_codified"])
        assert "(title not read)" in text
        assert _codes(sc)[-1] == CAVEAT_ORDINANCES_NOT_YET_CODIFIED
        assert "NEWER than everything listed above: 2 ordinance(s)" in text
        assert sc["ordinances"]  # the list itself is unchanged

    @pytest.mark.asyncio
    async def test_within_does_not_hide_them(self):
        sc, text = await self._recent(_plugin_with_pdfs(), within="Title 7")

        assert len(sc["not_yet_codified"]) == 2
        assert "`within` does not filter them" in text

    @pytest.mark.asyncio
    async def test_none_or_unknown_adds_nothing(self):
        for p in (_plugin_with_pdfs(hits=[]), _plugin_with_pdfs(search_status=500)):
            sc, _ = await self._recent(p)
            assert sc["not_yet_codified"] == []
            assert CAVEAT_ORDINANCES_NOT_YET_CODIFIED not in _codes(sc)


# ── 1.18.0: each pending ordinance carries its own title ─────────────────

from tests.test_ecode_chapters_and_attachments import make_pdf  # noqa: E402

# First pages of the three live ordinance PDFs, as pypdf returned them on
# 2026-11-20 (cut after the title). Each line ends with its drafting line
# number.
PAGE_108 = (
    " \n \nSubmitted by: Mayor LaFrance and \nAssembly Members \n"
    "Prepared by: Dept. of Law \nFor reading:  September 15, 2026 \n  \n"
    "ANCHORAGE, ALASKA \nAO No. 2026-108(S), As Amended \n \n"
    "(NOTE: This substitute version is presented without legislative "
    "drafting markup, except to \nthe title, from the text of the original "
    "AO. It is written as a new ordinance. See the AM for a \nsummary of "
    "changes.) \n \n"
    "AN ORDINANCE OF THE ANCHORAGE MUNICIPAL ASSEMBLY AMENDING 1 \n"
    "[REPEALING AND REPLACING] ANCHORAGE MUNICIPAL CODE CHAPTER 2 \n"
    "3.102 AND AMENDING AMC SECTION 7.15.040 TO IMPOSE NEW 3 \n"
    "RESTRICTIONS ON THE USE OF SURVEILLANCE TECHNOLOGY. 4 \n 5 \n"
    "WHEREAS, the Assembly finds 6 \n"
)
PAGE_98 = (
    " \n Submitted by: Assembly Chair Brawley \n For reading: October 20, "
    "2026 \n \nANCHORAGE, ALASKA \nAO No. 2026-98(S-1), As Amended \n \n"
    "AN ORDINANCE OF THE ANCHORAGE MUNICIPAL ASSEMBLY AMENDING 1 \n"
    "ANCHORAGE MUNICIPAL CODE CHAPTER 4.60 AND 27.30.085 TO ENACT A 2 \n"
    "NEW SECTION 4.60.310 AND ESTABLISH THE CHUGACH STATE PARK 3 \n"
    "ACCESS ADVISORY BOARD. 4 \n 5 \n"
    "WHEREAS, the natural beauty and recreational amenities of Chugach "
    "State 6 \nPark 7 \n"
)
PAGE_127 = (
    " \n     Submitted by: Chair of the Assembly at the  \n  Request of the "
    "Mayor \n      For reading: October 6, 2026    \n \nANCHORAGE, ALASKA "
    "\nAO No. 2026-127  \n \n"
    "AN ORDINANCE OF THE ANCHORAGE ASSEMBLY AMENDING ANCHORAGE 1 \n"
    "MUNICIPAL CODE CHAPTERS 1.35, 3.20, AND 3.30 RELATING TO THE 2 \n"
    "ORGANIZATION OF THE EXECUTIVE BRANCH. 3 \n 4 \n 5 \n"
    "WHEREAS, as outlined  in its 120 -day budget memorandum 6 \n"
)


class TestOrdinanceTitleFromPage:
    def test_line_numbers_are_dropped_and_the_title_kept_whole(self):
        assert EcodePlugin._ordinance_title_from_page(PAGE_98) == (
            "AN ORDINANCE OF THE ANCHORAGE MUNICIPAL ASSEMBLY AMENDING "
            "ANCHORAGE MUNICIPAL CODE CHAPTER 4.60 AND 27.30.085 TO ENACT A "
            "NEW SECTION 4.60.310 AND ESTABLISH THE CHUGACH STATE PARK "
            "ACCESS ADVISORY BOARD."
        )

    def test_a_chapter_number_split_across_lines_survives(self):
        """'... CODE CHAPTER 2' / '3.102 AND ...': the 2 is a line number,
        the chapter is 3.102."""
        title = EcodePlugin._ordinance_title_from_page(PAGE_108)

        assert "MUNICIPAL CODE CHAPTER 3.102 AND AMENDING" in title
        # Deleted words stay, in their brackets: nothing is edited out.
        assert "AMENDING [REPEALING AND REPLACING] ANCHORAGE" in title
        assert title.endswith("SURVEILLANCE TECHNOLOGY.")

    def test_stops_at_the_end_of_the_title(self):
        title = EcodePlugin._ordinance_title_from_page(PAGE_127)

        assert title.endswith("ORGANIZATION OF THE EXECUTIVE BRANCH.")
        assert "WHEREAS" not in title and "120" not in title

    def test_a_trailing_number_that_is_not_a_line_number_is_kept(self):
        page = (
            "AN ORDINANCE AMENDING ANCHORAGE MUNICIPAL CODE TITLE 21\n"
            "TO DO THINGS.\n"
        )
        assert EcodePlugin._ordinance_title_from_page(page) == (
            "AN ORDINANCE AMENDING ANCHORAGE MUNICIPAL CODE TITLE 21 TO DO "
            "THINGS."
        )

    @pytest.mark.parametrize(
        "page",
        ["", "A RESOLUTION OF THE ASSEMBLY.\n", "AN ORDINANCE with no end",
         "AN ORDINANCE.\n", "AN ORDINANCE " + "X " * 2000 + ".\n"],
    )
    def test_anything_else_is_not_guessed_at(self, page):
        assert EcodePlugin._ordinance_title_from_page(page) is None


@pytest.mark.ordinance_titles
class TestPendingOrdinanceTitles:
    """Prod, 2026-11-20: asked what changed in Title 7 and then Title 25,
    Claude read all three ordinance PDFs in full, twice, because the list
    gave only their numbers."""

    TITLES = {
        PDF_108: "AN ORDINANCE AMENDING CHAPTER 3.102 AND SECTION 7.15.040.",
        PDF_98: "AN ORDINANCE AMENDING CHAPTER 4.60 AND 27.30.085.",
    }

    def _plugin(self, fail=()):
        p = _plugin_with_pdfs()
        # Downloaded PDFs are kept at module level; start each test empty.
        ecode_mod._ATTACHMENT_CACHE.clear()
        fetched = []

        def handler(request):
            url = str(request.url)
            fetched.append(url)
            if url in fail:
                return httpx.Response(500)
            return httpx.Response(200, content=make_pdf([self.TITLES[url]]))

        p._attachment_transport = httpx.MockTransport(handler)
        p.fetched = fetched
        return p

    async def _recent(self, plugin, **args):
        result = await plugin.execute_tool("recent_ordinances", args)
        assert result.success, result.error_message
        sc = result.structured_content
        validate(sc, ORDINANCES_OUTPUT_SCHEMA)
        text = result.content[0]["text"]
        for caveat in sc["caveats"]:
            assert caveat["message"] in text
        return sc, text

    @pytest.mark.asyncio
    async def test_each_comes_with_its_title_and_what_it_names(self):
        sc, text = await self._recent(self._plugin())
        first, second = sc["not_yet_codified"]

        assert first["subject"] == self.TITLES[PDF_108]
        assert first["amends"] == ["3.102", "7.15.040"]
        assert second["amends"] == ["4.60", "27.30.085"]
        assert first["in_scope"] is None  # no `within`
        assert f"AO No. 2026-108(S) ({PDF_108}): {self.TITLES[PDF_108]}" in text

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "within, naming",
        [("Title 7", "AO No. 2026-108(S)"),
         ("Chapter 3.102", "AO No. 2026-108(S)"),
         ("Title 27", "AO No. 2026-98(S-1)"),
         ("Chapter 4.60", "AO No. 2026-98(S-1)")],
    )
    async def test_within_marks_the_ones_whose_title_names_it(
        self, within, naming
    ):
        sc, text = await self._recent(self._plugin(), within=within)
        first, second = sc["not_yet_codified"]

        # Shown first, and the other is still listed.
        assert (first["ordinance"], first["in_scope"]) == (naming, True)
        assert second["in_scope"] is False
        assert f"- [names it] {naming}" in text
        assert "can still amend it" in text

    @pytest.mark.asyncio
    async def test_within_a_title_none_of_them_names(self):
        sc, text = await self._recent(self._plugin(), within="Title 21")

        assert [o["in_scope"] for o in sc["not_yet_codified"]] == [False, False]
        assert "0 of them name(s) Title 21" in text

    @pytest.mark.asyncio
    async def test_a_pdf_that_cannot_be_read_is_listed_without_a_title(self):
        sc, text = await self._recent(
            self._plugin(fail={PDF_98}), within="Title 4"
        )
        by_name = {o["ordinance"]: o for o in sc["not_yet_codified"]}

        assert by_name["AO No. 2026-98(S-1)"]["subject"] is None
        assert by_name["AO No. 2026-98(S-1)"]["in_scope"] is None
        assert by_name["AO No. 2026-108(S)"]["in_scope"] is False
        assert "(title not read)" in text

    @pytest.mark.asyncio
    async def test_titles_are_read_once_per_container(self):
        p = self._plugin()
        await self._recent(p)
        await self._recent(p, within="Title 7")
        await _find("2026-108", p)

        assert sorted(p.fetched) == sorted([PDF_108, PDF_98])

    @pytest.mark.asyncio
    async def test_find_ordinance_shows_the_title(self):
        sc, text = await _find("AO 2026-108(S)", self._plugin())
        (pdf,) = sc["ordinance_pdfs"]

        assert pdf["subject"] == self.TITLES[PDF_108]
        assert pdf["amends"] == ["3.102", "7.15.040"]
        assert f"its title: {self.TITLES[PDF_108]}" in text

    @pytest.mark.asyncio
    async def test_slow_titles_never_hold_up_the_result(self, monkeypatch):
        import asyncio as aio

        async def slow(self, url):
            await aio.sleep(30)

        monkeypatch.setattr(EcodePlugin, "_ordinance_title", slow)
        monkeypatch.setattr(ecode_mod, "ORDINANCE_TITLES_TIMEOUT_S", 0.1)
        sc, _ = await self._recent(self._plugin())

        assert len(sc["not_yet_codified"]) == 2
        assert all(o["subject"] is None for o in sc["not_yet_codified"])


# ── 1.19.0: sections that cite an ordinance the table does not list ──────

from plugins.ecode.plugin import CAVEAT_ORDINANCE_CITING_SECTIONS  # noqa: E402


def _cite_hit(guid, number, title, text, **extra):
    return {"id": guid, "number": number, "title": title, "text": text,
            "type": "code", "pdf": False, **extra}


# The live search for "2026-40" (2026-11-20): six sections whose history
# note cites it, the Ordinance List row, and an ordinance PDF that only
# mentions the number.
CITING_2026_40 = [
    _cite_hit("s71", "§\xa07.10.010", "General provisions.",
              '...(S) Amended , 1-13-2026 ; AO No. <em class="highlight">'
              "2026-40</em>(S-1) Amended , 6-23-2026 )"),
    _cite_hit("s72", "§\xa07.20.120", "Emergency procurements.",
              "...to support the annual report. ( AO No. 2026-40(S-1) "
              "Amended , 6-23-2026 ) [..."),
    _cite_hit("ol", "§\xa0OL.010", "Ordinance List and Disposition Table",
              "...Ch. 7.20 0.1 2026-40(S-1) Amended 6-23-2026 Amends..."),
    {"id": "LF2907762", "type": "law", "pdf": True, "number": "",
     "title": "AO No. 2026-108(S)", "text": "...as amended by AO No. 2026-40...",
     "url": "https://live.ecode360.com/AN6998/laws/LF2907762.pdf"},
    _cite_hit("s73", "§\xa07.20.070", "Sole source.",
              "...-37 , § 16, 5-16-2025 ; AO No. 2026-40(S-1) Amended , "
              "6-23-2026 )"),
]


def _plugin_citing(hits_by_query, total=None, **kwargs):
    """`hits_by_query`: {query: [hits]}; pages of 20; any other search
    returns nothing."""
    p = _plugin(**kwargs)
    inner = p.client.get.side_effect
    queries = []

    async def fake_get(path, params=None, **kw):
        if path.endswith("/search"):
            queries.append((params["query"], int(params["page"])))
            hits = hits_by_query.get(params["query"], [])
            page = int(params["page"])
            return httpx.Response(200, request=REQ, json={
                "results": hits[page * 20:(page + 1) * 20],
                "page": str(page), "maxPages": 9,
                "totalResultCount": len(hits) if total is None else total})
        return await inner(path, params=params, **kw)

    p.client.get = AsyncMock(side_effect=fake_get)
    p.queries = queries
    return p


class TestCitingSections:
    """Prod, 2026-11-20: find_ordinance('AO 2026-40') said 'in the recent
    list, sections not listed yet', and the caller then searched for the
    number to find the six sections that cite it."""

    @pytest.mark.asyncio
    async def test_recent_ordinance_gets_the_sections_that_cite_it(self):
        p = _plugin_citing({'"2026-40"': CITING_2026_40})
        sc, text = await _find("AO 2026-40", p)

        assert sc["found"] is False
        assert [r["guid"] for r in sc["citing_sections"]] == ["s71", "s72", "s73"]
        first = sc["citing_sections"][0]
        assert first == {
            "guid": "s71",
            "ref": "§ 7.10.010",
            "title": "General provisions.",
            "url": "https://ecode360.com/s71",
            "cited_as": "AO No. 2026-40(S-1) Amended, 6-23-2026",
        }
        assert sc["citing_sections_complete"] is True
        assert sc["summary"] == (
            "AO 2026-40(S-1) Amended is in the list of recent ordinances; "
            "the Ordinance Disposition Table does not list it yet, but 3 "
            "section(s) of the code cite it."
        )
        assert text.startswith(sc["summary"])
        assert CAVEAT_ORDINANCE_CITING_SECTIONS in _codes(sc)
        assert "- § 7.20.070 Sole source. -- AO No. 2026-40(S-1) Amended, 6-23-2026" in text
        assert sc["listing"]["adopted"] == "2026-06-23"

    @pytest.mark.asyncio
    async def test_tables_pdfs_and_bare_mentions_are_not_sections(self):
        p = _plugin_citing({'"2026-40"': CITING_2026_40})
        sc, _ = await _find("2026-40", p)
        guids = [r["guid"] for r in sc["citing_sections"]]

        assert "ol" not in guids  # the Ordinance List row: no "AO" before it
        assert "LF2907762" not in guids  # a PDF

    @pytest.mark.asyncio
    async def test_older_citation_form_without_no_is_found(self):
        """2012-2014 notes write 'AO 2013-117'; the old suggested search,
        '"AO No. 2013-117"', found 1 section where the number finds 68."""
        hits = [_cite_hit("a", "§\xa021.02.040", "T",
                          "...2012-124(S) , 2-26-2013 ; AO 2031-7 , 12-3-2031 )")]
        sc, _ = await _find("2031-7", _plugin_citing({'"2031-7"': hits}))

        assert sc["search_query"] == '"2031-7"'
        assert sc["citing_sections"][0]["cited_as"] == "AO 2031-7, 12-3-2031"
        assert sc["summary"] == (
            "AO 2031-7 is not in the Ordinance Disposition Table, but 1 "
            "section(s) of the code cite it."
        )

    @pytest.mark.asyncio
    async def test_a_longer_number_is_not_this_ordinance(self):
        hits = [_cite_hit("a", "§\xa01.05.010", "T", "( AO No. 2031-70 , 1-1-2031 )"),
                _cite_hit("b", "§\xa01.05.020", "T", "( AR No. 2031-7 , 1-1-2031 )")]
        sc, _ = await _find("2031-7", _plugin_citing({'"2031-7"': hits}))

        assert sc["citing_sections"] == []
        assert CAVEAT_ORDINANCE_CITING_SECTIONS not in _codes(sc)

    @pytest.mark.asyncio
    async def test_no_search_when_the_table_lists_it(self):
        p = _plugin_citing({'"2025-115"': CITING_2026_40})
        sc, _ = await _find("2025-115", p)

        assert sc["found"] is True
        assert sc["citing_sections"] == []
        assert ('"2025-115"', 0) not in p.queries

    @pytest.mark.asyncio
    async def test_more_hits_than_are_checked_says_so(self):
        hits = [
            _cite_hit(f"g{i}", f"§\xa01.05.{i:03d}", "T",
                      "( AO No. 2031-7 , 1-1-2031 )")
            for i in range(75)
        ]
        p = _plugin_citing({'"2031-7"': hits})
        sc, text = await _find("2031-7", p)

        assert len(sc["citing_sections"]) == 60
        assert sc["citing_sections_complete"] is False
        assert "60 or more section(s)" in sc["summary"]
        assert "Only the first 60 search hits were checked" in text
        assert [q for q in p.queries if q[0] == '"2031-7"'] == [
            ('"2031-7"', 0), ('"2031-7"', 1), ('"2031-7"', 2),
        ]

    @pytest.mark.asyncio
    async def test_exactly_three_full_pages_is_complete(self):
        hits = [
            _cite_hit(f"g{i}", f"§\xa01.05.{i:03d}", "T",
                      "( AO No. 2031-7 , 1-1-2031 )")
            for i in range(60)
        ]
        sc, _ = await _find("2031-7", _plugin_citing({'"2031-7"': hits}))

        assert len(sc["citing_sections"]) == 60
        assert sc["citing_sections_complete"] is True

    @pytest.mark.asyncio
    async def test_a_failed_search_changes_nothing(self):
        sc, text = await _find("2031-5")  # this plugin's searches 404

        assert sc["citing_sections"] == []
        assert sc["citing_sections_complete"] is True
        assert sc["summary"] == (
            "AO 2031-5 is not listed in the Ordinance Disposition Table."
        )

    @pytest.mark.asyncio
    async def test_citation_stops_at_its_date(self):
        """A snippet can join two excerpts: '... AO No. 2031-7 , 10-20-2031
        Spenard Corridor Plan.... 2031-6 , 10-7-2031'."""
        hits = [_cite_hit("a", "§\xa021.01.080", "Comprehensive plan.",
                          "... ; AO No. 2031-7 , 10-20-2031 Spenard Corridor "
                          "Plan.... 2031-6 , 10-7-2031 ; AO No. 2031-7 , "
                          "10-20-2031 )")]
        sc, _ = await _find("2031-7", _plugin_citing({'"2031-7"': hits}))

        assert sc["citing_sections"][0]["cited_as"] == "AO No. 2031-7, 10-20-2031"
