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
        assert sc["search_query"] == '"AO No. 2025-115"'

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
        assert '"AO No. 2031-5"' in text

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
