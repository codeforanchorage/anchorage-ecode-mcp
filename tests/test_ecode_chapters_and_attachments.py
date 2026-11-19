"""Chapter / title references and the PDF attachment reader.

Both come out of the 2026-11-15 prod-log review:

* get_section('17.05') failed because the API knows the chapter only as
  "Ch 17.05" -- and the Charter has a "Section 17.05" too, so a bare
  two-part number is genuinely ambiguous.
* A model tried to open the Table 21.05-1 PDF by handing its file name to
  get_section; the zoning use tables are published only as PDFs.
"""

import httpx
import pytest
from jsonschema import validate
from unittest.mock import AsyncMock, MagicMock

import plugins.ecode.plugin as ecode_mod
from plugins.ecode.config_schema import EcodePluginConfig
from plugins.ecode.plugin import (
    ATTACHMENT_OUTPUT_SCHEMA,
    CAVEAT_MORE_ATTACHMENT_PAGES,
    CAVEAT_NO_TEXT_LAYER,
    CAVEAT_NODE_TOO_LARGE,
    CAVEAT_PDF_TEXT_EXTRACTION,
    CAVEAT_RENDERED_TEXT_TRUNCATED,
    CAVEAT_RESOLVED_FROM_SECTION_NUMBER,
    KIND_CHAPTER,
    KIND_SECTION,
    KIND_TITLE,
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


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch):
    monkeypatch.setattr(ecode_mod, "RETRY_BACKOFF_S", 0.0)


@pytest.fixture(autouse=True)
def clear_attachment_cache():
    ecode_mod._ATTACHMENT_CACHE.clear()
    yield
    ecode_mod._ATTACHMENT_CACHE.clear()


def _json(body, status=200):
    return httpx.Response(status_code=status, json=body, request=REQ)


def _node(guid, label, number, title, type_="section", index=None, kids=()):
    return {
        "guid": guid,
        "label": label,
        "number": number,
        "indexNum": index if index is not None else number,
        "title": title,
        "type": type_,
        "children": list(kids),
    }


# The live outline's shape (2026-11-15), cut down: the Charter's sections
# are numbered like the code's chapters, which is the whole difficulty.
TOC = {
    "guid": "AN6998",
    "type": "code",
    "label": "",
    "number": "",
    "title": "Municipality of Anchorage, AK",
    "children": [
        _node("p1", "Part", "I", "PART I: CHARTER", "division", "", [
            _node("48977220", "Title", "CHA", "Charter", "chapter",
                  "Ttl CHA", [
                _node("a17", "ARTICLE", "XVII", "GENERAL", "article",
                      "Art CHA-XVII", [
                    _node("48977514", "Section", "Section\xa017.05",
                          "Public meetings.", "section", "§\xa017.05"),
                    _node("48977999", "Section", "Section\xa01.01",
                          "Name.", "section", "§\xa01.01"),
                ]),
            ]),
        ]),
        _node("p2", "", "II", "PART II: CODE", "division", "", [
            _node("49401700", "Title", "17", "Animals", "chapter",
                  "Ttl 17", [
                _node("49401712", "Chapter", "17.05", "GENERAL PROVISIONS",
                      "article", "Ch 17.05", [
                    _node("49401720", "Section", "§\xa017.05.010",
                          "Definitions.", "section"),
                ]),
            ]),
            _node("49419884", "Title", "21", "Land Use Planning", "chapter",
                  "Ttl 21", [
                _node("49422043", "Chapter", "21.05", "USE REGULATIONS",
                      "article", "Ch 21.05"),
            ]),
            _node("t23", "Title", "23", "Building Codes", "chapter",
                  "Ttl 23", [
                _node("c2310", "Chapter", "23.10", "ADMIN", "article",
                      "Ch 23.10", [
                    # labelled Section, but a part of the building code
                    _node("sub101", "Section", "101", "General",
                          "subarticle", "Ch 23.10 § 101"),
                ]),
            ]),
        ]),
        _node("p3", "Part", "III", "PART III: REGULATIONS", "division", "", [
            _node("48977659", "Regulation", "R3.70", "EMPLOYEE RELATIONS",
                  "article", "R3.70"),
        ]),
    ],
}


def _content(guid, number="", title="T", content="Body.", **extra):
    return {
        "id": guid,
        "number": number,
        "title": title,
        "type": "container",
        "content": content,
        "children": [],
        **extra,
    }


def _plugin(content=None, searches=None, toc=TOC, toc_status=200):
    content = content or {}
    searches = searches or {}
    calls = []

    async def fake_get(path, params=None, **_kw):
        calls.append((path, params))
        if path.endswith("/code/toc"):
            return _json(toc if toc_status == 200 else {"message": "x"},
                         toc_status)
        if path.endswith("/search"):
            return _json(searches.get(params["query"], {"results": []}))
        guid = path.rsplit("/", 1)[-1]
        if guid in content:
            return _json(content[guid])
        return _json({"message": f"Could not find Content: {guid}"}, 404)

    p = EcodePlugin(CONFIG)
    p.plugin_config = EcodePluginConfig(**CONFIG)
    p.client = MagicMock()
    p.client.get = AsyncMock(side_effect=fake_get)
    p.calls = calls
    return p


# ── Reference parsing ──────────────────────────────────────────────────


class TestParseRef:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("21.05.010", (None, "21.05.010", False)),
            ("§ 21.05.010", (None, "21.05.010", False)),
            ("AMC 21.05.010", (None, "21.05.010", False)),
            ("17.05", (None, "17.05", False)),
            ("R3.70", (None, "R3.70", False)),
            ("Chapter 17.05", (KIND_CHAPTER, "17.05", False)),
            ("ch. 17.05", (KIND_CHAPTER, "17.05", False)),
            ("Ch 21A", (KIND_CHAPTER, "21A", False)),
            ("Title 21", (KIND_TITLE, "21", False)),
            ("Ttl CHA", (KIND_TITLE, "CHA", False)),
            ("Section 17.05", (KIND_SECTION, "17.05", False)),
            ("Sec. 21.05.010", (KIND_SECTION, "21.05.010", False)),
            ("Charter Section 17.05", (KIND_SECTION, "17.05", True)),
            ("charter § 17.05", (KIND_SECTION, "17.05", True)),
            # A kind word needs a space after it: these are whole numbers.
            ("CHA-CT.010", (None, "CHA-CT.010", False)),
            ("CCT.020", (None, "CCT.020", False)),
        ],
    )
    def test_references(self, raw, expected):
        assert EcodePlugin._parse_ref(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        [
            "49422044",  # a guid
            "ROOT",
            "21",  # a bare integer is a guid, never Title 21
            "Section 21",  # a section needs a dotted number
            "fence",
            "Ttl 21: AN6998-021a Table 21.05-1 Uses.pdf",
            "",
            None,
            21.05,
        ],
    )
    def test_everything_else_is_left_to_the_guid_check(self, raw):
        assert EcodePlugin._parse_ref(raw) is None


# ── Chapter / title resolution ─────────────────────────────────────────


class TestChapterResolution:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "arg,guid",
        [
            ("Chapter 17.05", "49401712"),
            ("ch 17.05", "49401712"),
            ("Title 21", "49419884"),
            ("Ttl 21", "49419884"),
            ("Charter Section 17.05", "48977514"),
            ("Section 17.05", "48977514"),
            ("21.05", "49422043"),  # unique: no Charter Section 21.05 here
            ("R3.70", "48977659"),
            ("Section 1.01", "48977999"),
        ],
    )
    async def test_resolves_from_the_outline(self, arg, guid):
        p = _plugin(content={guid: _content(guid)})
        result = await p.execute_tool("get_section", {"guid": arg})

        assert result.success is True, result.error_message
        sc = result.structured_content
        validate(sc, SECTION_OUTPUT_SCHEMA)
        assert sc["guid"] == guid
        codes = [c["code"] for c in sc["caveats"]]
        assert CAVEAT_RESOLVED_FROM_SECTION_NUMBER in codes
        # outline + content, and never a search
        assert [c[0].rsplit("/", 1)[-1] for c in p.calls] == ["toc", guid]

    @pytest.mark.asyncio
    async def test_bare_number_shared_with_the_charter_is_ambiguous(self):
        p = _plugin()
        result = await p.execute_tool("get_section", {"guid": "17.05"})

        assert result.success is False
        msg = result.error_message
        assert "matches 2 different entries" in msg
        assert "Chapter 17.05 GENERAL PROVISIONS (guid 49401712)" in msg
        assert "Charter Section 17.05 Public meetings. (guid 48977514)" in msg
        assert "'Chapter 17.05'" in msg and "'Charter Section 17.05'" in msg
        # nothing was fetched on a guess
        assert all(not c[0].endswith(("49401712", "48977514"))
                   for c in p.calls)

    @pytest.mark.asyncio
    async def test_bare_number_never_means_a_whole_title(self):
        """'23.10' must not match Title anything, and the building-code
        part labelled Section 101 is not a section."""
        p = _plugin(content={"c2310": _content("c2310")})
        result = await p.execute_tool("get_section", {"guid": "23.10"})

        assert result.structured_content["guid"] == "c2310"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "arg,what",
        [
            ("Chapter 99.99", "chapter"),
            ("Title 99", "title"),
            ("Charter Section 99.99", "Charter section"),
        ],
    )
    async def test_unknown_explicit_reference_is_named(self, arg, what):
        p = _plugin()
        result = await p.execute_tool("get_section", {"guid": arg})

        assert result.success is False
        assert f"No {what} numbered" in result.error_message
        assert "browse_structure" in result.error_message
        assert len(p.calls) == 1  # the outline only

    @pytest.mark.asyncio
    async def test_bare_number_not_in_outline_falls_back_to_search(self):
        search = {"results": [{"id": "777", "number": "§ 5.20"}]}
        p = _plugin(
            content={"777": _content("777")}, searches={"5.20": search}
        )
        result = await p.execute_tool("get_section", {"guid": "5.20"})

        assert result.success is True
        assert result.structured_content["guid"] == "777"

    @pytest.mark.asyncio
    async def test_outline_down_bare_number_still_uses_search(self):
        search = {"results": [{"id": "49422043", "number": "21.05"}]}
        p = _plugin(
            content={"49422043": _content("49422043")},
            searches={"21.05": search},
            toc_status=500,
        )
        result = await p.execute_tool("get_section", {"guid": "21.05"})

        assert result.success is True, result.error_message

    @pytest.mark.asyncio
    async def test_outline_down_explicit_reference_is_an_upstream_error(self):
        p = _plugin(toc_status=500)
        result = await p.execute_tool("get_section", {"guid": "Title 21"})

        assert result.success is False
        assert "HTTP 500" in result.error_message

    @pytest.mark.asyncio
    async def test_three_part_number_still_resolves_by_search(self):
        search = {"results": [{"id": "49401720", "number": "§ 17.05.010"}]}
        p = _plugin(
            content={"49401720": _content("49401720")},
            searches={"17.05.010": search},
        )
        result = await p.execute_tool("get_section", {"guid": "17.05.010"})

        assert result.success is True
        assert not any(c[0].endswith("/code/toc") for c in p.calls)

    @pytest.mark.asyncio
    async def test_batch_of_chapters_downloads_the_outline_once(self):
        guids = ["49401712", "49422043", "48977659", "49419884"]
        p = _plugin(content={g: _content(g) for g in guids})
        result = await p.execute_tool(
            "get_sections",
            {"sections": ["Chapter 17.05", "21.05", "R3.70", "Title 21"]},
        )

        sc = result.structured_content
        validate(sc, SECTIONS_OUTPUT_SCHEMA)
        assert [s["guid"] for s in sc["sections"]] == guids
        assert sum(c[0].endswith("/code/toc") for c in p.calls) == 1


# ── Whole chapters and titles are bounded ──────────────────────────────


class TestLargeNodes:
    @pytest.mark.asyncio
    async def test_a_huge_title_is_cut_and_points_at_the_outline(self):
        cap = ecode_mod.MAX_STRUCTURED_CHARS
        children = [
            {"number": f"§ {i}", "title": "S", "content": "y" * 10_000,
             "children": []}
            for i in range(cap // 10_000 + 5)
        ]
        big = _content("49419884", "21", "Land Use Planning", content="",
                       children=children)
        p = _plugin(content={"49419884": big})
        result = await p.execute_tool("get_section", {"guid": "49419884"})
        sc = result.structured_content

        validate(sc, SECTION_OUTPUT_SCHEMA)
        codes = [c["code"] for c in sc["caveats"]]
        assert CAVEAT_NODE_TOO_LARGE in codes
        # one caveat about size, not two that contradict each other
        assert CAVEAT_RENDERED_TEXT_TRUNCATED not in codes
        assert cap * 0.9 < len(sc["text"]) < cap * 1.1
        message = next(
            c["message"] for c in sc["caveats"]
            if c["code"] == CAVEAT_NODE_TOO_LARGE
        )
        assert "browse_structure" in message and "49419884" in message
        assert message in result.content[0]["text"]

    @pytest.mark.asyncio
    async def test_an_ordinary_long_section_is_still_whole(self):
        body = "x" * 61_000  # the largest real section seen in prod
        p = _plugin(content={"1": _content("1", content=body)})
        sc = (
            await p.execute_tool("get_section", {"guid": "1"})
        ).structured_content

        assert sc["text"] == body
        codes = [c["code"] for c in sc["caveats"]]
        assert CAVEAT_NODE_TOO_LARGE not in codes
        assert CAVEAT_RENDERED_TEXT_TRUNCATED in codes


# ── Attachments listed by get_section ──────────────────────────────────


class TestAttachmentListing:
    @pytest.mark.asyncio
    async def test_attachment_urls_are_surfaced(self):
        url = "https://ecode360.com/documents/AN6998/AN6998-021a Table.pdf"
        node = _content(
            "49419884", "21", "Land Use Planning",
            attachments={"Attachment 1 - Table 21.05-1": url, "Rel": "/d/x.pdf"},
        )
        p = _plugin(content={"49419884": node})
        result = await p.execute_tool("get_section", {"guid": "49419884"})
        sc = result.structured_content

        validate(sc, SECTION_OUTPUT_SCHEMA)
        assert sc["attachments"] == [
            {"name": "Attachment 1 - Table 21.05-1", "url": url},
            {"name": "Rel", "url": "https://ecode360.com/d/x.pdf"},
        ]
        assert url in result.content[0]["text"]
        assert "read_attachment" in result.content[0]["text"]

    @pytest.mark.asyncio
    async def test_attachment_file_name_as_guid_points_at_read_attachment(
        self,
    ):
        """Seen in prod: the search hit's id for a PDF is its file name."""
        p = _plugin()
        result = await p.execute_tool(
            "get_section",
            {"guid": "Ttl 21: AN6998-021a Table 21.05-1 Table of Uses.pdf"},
        )

        assert result.success is False
        assert "PDF attachment" in result.error_message
        assert "read_attachment" in result.error_message
        assert "section NUMBER" not in result.error_message
        assert p.calls == []


# ── read_attachment ────────────────────────────────────────────────────


def make_pdf(pages):
    """A minimal valid PDF with one line of Helvetica text per page ('' for
    a page with no text at all)."""
    objs = []

    def add(body):
        objs.append(body)
        return len(objs)

    add(b"<< /Type /Catalog /Pages 2 0 R >>")
    add(b"")  # /Pages, filled in below
    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    kids = []
    for text in pages:
        stream = (
            f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
            if text
            else b""
        )
        content = add(
            b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream)
        )
        kids.append(add(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 %d 0 R >> >> /Contents %d 0 R >>"
            % (font, content)
        ))
    objs[1] = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (
        b" ".join(b"%d 0 R" % k for k in kids), len(kids)
    )
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (i, body)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += (
        b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n"
        % (len(objs) + 1, xref)
    )
    return bytes(out)


GOOD_URL = (
    "https://live.ecode360.com/attachment/339626/"
    "AN6998-021a Table 21.05-1 Table of Allowed Uses.pdf"
)


def _attachment_plugin(handler):
    p = EcodePlugin(CONFIG)
    p.plugin_config = EcodePluginConfig(**CONFIG)
    p.client = MagicMock()
    p.client.get = AsyncMock(side_effect=AssertionError("API not expected"))
    seen = []

    def wrapped(request):
        seen.append(request)
        return handler(request)

    p._attachment_transport = httpx.MockTransport(wrapped)
    p.requests = seen
    return p


def _pdf_response(pages):
    def handler(_request):
        return httpx.Response(
            200, content=make_pdf(pages),
            headers={"content-type": "application/pdf"},
        )
    return handler


class TestAttachmentUrl:
    @pytest.mark.parametrize(
        "url",
        [
            GOOD_URL,
            GOOD_URL.replace(" ", "%20"),
            "https://ecode360.com/documents/AN6998/AN6998-021a Table.pdf",
            "https://live.ecode360.com/documents/AN6998/public/75325.PDF",
            "HTTPS://LIVE.ECODE360.COM/attachment/1/a.pdf".replace(
                "HTTPS", "https"
            ),
        ],
    )
    def test_ecode360_pdf_urls_are_accepted(self, url):
        p = _attachment_plugin(_pdf_response(["x"]))
        out = p._attachment_url_arg({"url": url})

        assert out.startswith("https://")
        assert " " not in out
        assert out.lower().endswith(".pdf")

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "url",
        [
            None,
            "",
            7,
            "http://live.ecode360.com/attachment/1/a.pdf",
            "https://evil.example/attachment/1/a.pdf",
            "https://live.ecode360.com.evil.example/attachment/1/a.pdf",
            "https://evil.example@live.ecode360.com/attachment/1/a.pdf",
            "https://live.ecode360.com:8443/attachment/1/a.pdf",
            "https://live.ecode360.com/attachment/1/a.pdf?x=1",
            "https://live.ecode360.com/attachment/1/../../etc/a.pdf",
            "https://live.ecode360.com/attachment/1/%2e%2e/a.pdf",
            "https://live.ecode360.com/attachment/1/a.html",
            "https://live.ecode360.com/attachment/abc/a.pdf",
            "https://live.ecode360.com/49422044",
            # another city's library
            "https://ecode360.com/documents/VA4680/a.pdf",
            "https://api.ecode360.com/v1/customer/AN6998/a.pdf",
            "https://169.254.169.254/attachment/1/a.pdf",
            "file:///etc/passwd.pdf",
            "49422044",
        ],
    )
    async def test_anything_else_is_refused_before_any_request(self, url):
        p = _attachment_plugin(_pdf_response(["x"]))
        args = {} if url is None else {"url": url}
        result = await p.execute_tool("read_attachment", args)

        assert result.success is False
        assert "url" in result.error_message
        assert p.requests == []


class TestReadAttachment:
    @pytest.mark.asyncio
    async def test_returns_page_text_and_conforms(self):
        p = _attachment_plugin(_pdf_response(["Alpha uses", "Beta uses"]))
        result = await p.execute_tool("read_attachment", {"url": GOOD_URL})

        assert result.success is True, result.error_message
        sc = result.structured_content
        validate(sc, ATTACHMENT_OUTPUT_SCHEMA)
        assert sc["page_count"] == 2
        assert [pg["page"] for pg in sc["pages"]] == [1, 2]
        assert "Alpha uses" in sc["pages"][0]["text"]
        assert "Beta uses" in sc["pages"][1]["text"]
        assert sc["next_page"] is None
        assert sc["filename"].endswith("Table of Allowed Uses.pdf")
        assert sc["layout"] is True
        text = result.content[0]["text"]
        assert "--- page 2 of 2 ---" in text
        for caveat in sc["caveats"]:
            assert caveat["message"] in text

    @pytest.mark.asyncio
    async def test_every_result_says_it_is_extracted_pdf_text(self):
        p = _attachment_plugin(_pdf_response(["x"]))
        sc = (
            await p.execute_tool("read_attachment", {"url": GOOD_URL})
        ).structured_content

        caveat = sc["caveats"][0]
        assert caveat["code"] == CAVEAT_PDF_TEXT_EXTRACTION
        assert sc["url"] in caveat["message"]

    @pytest.mark.asyncio
    async def test_api_credentials_are_never_sent_to_the_website(self):
        p = _attachment_plugin(_pdf_response(["x"]))
        await p.execute_tool("read_attachment", {"url": GOOD_URL})

        (request,) = p.requests
        assert request.url.host == "live.ecode360.com"
        assert "api-key" not in request.headers
        assert "api-secret" not in request.headers
        assert "%20" in str(request.url)

    @pytest.mark.asyncio
    async def test_pages_past_the_budget_are_offered_as_next_page(
        self, monkeypatch
    ):
        monkeypatch.setattr(ecode_mod, "ATTACHMENT_TEXT_CHARS", 25)
        p = _attachment_plugin(
            _pdf_response(["page one text", "page two text", "page three"])
        )
        first = (
            await p.execute_tool("read_attachment", {"url": GOOD_URL})
        ).structured_content

        assert [pg["page"] for pg in first["pages"]] == [1]
        assert first["next_page"] == 2
        codes = [c["code"] for c in first["caveats"]]
        assert CAVEAT_MORE_ATTACHMENT_PAGES in codes

        rest = (
            await p.execute_tool(
                "read_attachment", {"url": GOOD_URL, "page": 2}
            )
        ).structured_content
        assert rest["pages"][0]["page"] == 2
        # paging reuses the download in a warm container
        assert len(p.requests) == 1

    @pytest.mark.asyncio
    async def test_spent_time_budget_still_returns_one_page(self):
        pdf = make_pdf(["one", "two", "three"])
        out = EcodePlugin._extract_pdf(pdf, 1, True, deadline=0.0)

        assert [pg["page"] for pg in out["pages"]] == [1]
        assert out["next_page"] == 2

    @pytest.mark.asyncio
    async def test_page_past_the_end_is_a_caller_error(self):
        p = _attachment_plugin(_pdf_response(["only"]))
        result = await p.execute_tool(
            "read_attachment", {"url": GOOD_URL, "page": 5}
        )

        assert result.success is False
        assert "1 page(s)" in result.error_message

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "args,needle",
        [
            ({"page": 0}, "page must be 1 or greater"),
            ({"page": "two"}, "page must be an integer"),
            ({"layout": "yes"}, "layout must be true or false"),
        ],
    )
    async def test_bad_arguments_are_caller_errors(self, args, needle):
        p = _attachment_plugin(_pdf_response(["x"]))
        result = await p.execute_tool(
            "read_attachment", {"url": GOOD_URL, **args}
        )

        assert result.success is False
        assert needle in result.error_message
        assert p.requests == []

    @pytest.mark.asyncio
    async def test_page_without_text_is_flagged(self):
        p = _attachment_plugin(_pdf_response(["words", ""]))
        sc = (
            await p.execute_tool("read_attachment", {"url": GOOD_URL})
        ).structured_content

        validate(sc, ATTACHMENT_OUTPUT_SCHEMA)
        flagged = [
            c["message"] for c in sc["caveats"]
            if c["code"] == CAVEAT_NO_TEXT_LAYER
        ]
        assert flagged and "2" in flagged[0]

    @pytest.mark.asyncio
    async def test_plain_mode_warns_against_reading_table_cells(self):
        p = _attachment_plugin(_pdf_response(["x"]))
        sc = (
            await p.execute_tool(
                "read_attachment", {"url": GOOD_URL, "layout": False}
            )
        ).structured_content

        assert sc["layout"] is False
        assert "do not read table cells" in sc["caveats"][0]["message"]

    @pytest.mark.asyncio
    async def test_missing_attachment_is_a_caller_error(self):
        p = _attachment_plugin(lambda _r: httpx.Response(404, text="nope"))
        result = await p.execute_tool("read_attachment", {"url": GOOD_URL})

        assert result.success is False
        assert "No attachment exists" in result.error_message

    @pytest.mark.asyncio
    async def test_redirect_is_not_followed(self):
        def handler(request):
            assert request.url.host == "live.ecode360.com"
            return httpx.Response(
                302, headers={"location": "https://evil.example/x.pdf"}
            )

        p = _attachment_plugin(handler)
        result = await p.execute_tool("read_attachment", {"url": GOOD_URL})

        assert result.success is False
        assert "HTTP 302" in result.error_message
        assert len(p.requests) == 1

    @pytest.mark.asyncio
    async def test_non_pdf_body_is_refused(self):
        p = _attachment_plugin(
            lambda _r: httpx.Response(200, text="<html>login</html>")
        )
        result = await p.execute_tool("read_attachment", {"url": GOOD_URL})

        assert result.success is False
        assert "not a PDF" in result.error_message
        assert ecode_mod._ATTACHMENT_CACHE == {}

    @pytest.mark.asyncio
    async def test_oversized_download_is_stopped(self, monkeypatch):
        monkeypatch.setattr(ecode_mod, "MAX_ATTACHMENT_BYTES", 100)
        p = _attachment_plugin(_pdf_response(["x" * 50, "y" * 50]))
        result = await p.execute_tool("read_attachment", {"url": GOOD_URL})

        assert result.success is False
        assert "larger than" in result.error_message

    @pytest.mark.asyncio
    async def test_corrupt_pdf_is_a_readable_error(self):
        p = _attachment_plugin(
            lambda _r: httpx.Response(200, content=b"%PDF-1.4\ngarbage")
        )
        result = await p.execute_tool("read_attachment", {"url": GOOD_URL})

        assert result.success is False
        assert "could not be opened as a PDF" in result.error_message

    @pytest.mark.asyncio
    async def test_timeout_is_named(self):
        def handler(request):
            raise httpx.ReadTimeout("", request=request)

        p = _attachment_plugin(handler)
        result = await p.execute_tool("read_attachment", {"url": GOOD_URL})

        assert result.success is False
        assert "did not deliver the attachment" in result.error_message

    def test_cache_holds_only_a_couple_of_documents(self):
        assert ecode_mod.ATTACHMENT_CACHE_ENTRIES <= 3


# ── 1.15.0: ordinance PDFs ───────────────────────────────────────────────

LAW_URL = "https://live.ecode360.com/AN6998/laws/LF2907762.pdf"


class TestOrdinancePdf:
    """Search returns recently adopted ordinances as PDFs and tells the
    caller to pass the link to read_attachment, which used to refuse it."""

    @pytest.mark.parametrize(
        "url",
        [
            LAW_URL,
            "https://ecode360.com/AN6998/laws/LF1.pdf",
            "https://live.ecode360.com/an6998/laws/lf2907762.PDF",
        ],
    )
    def test_url_is_accepted(self, url):
        p = _attachment_plugin(_pdf_response(["x"]))
        assert p._attachment_url_arg({"url": url}).startswith("https://")
        assert p._is_ordinance_pdf(url) is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "url",
        [
            "https://live.ecode360.com/VA4680/laws/LF2907762.pdf",  # other city
            "https://live.ecode360.com/AN6998/laws/2907762.pdf",
            "https://live.ecode360.com/AN6998/laws/LFabc.pdf",
            "https://live.ecode360.com/AN6998/laws/LF1.html",
            "https://live.ecode360.com/AN6998/laws/x/LF1.pdf",
            "https://live.ecode360.com/AN6998/laws/../laws/LF1.pdf",
            "https://live.ecode360.com/AN6998/laws/LF1.pdf?x=1",
            "https://live.ecode360.com/AN6998/law/LF1.pdf",
            "https://live.ecode360.com/AN6998/LF1.pdf",
            "https://evil.example/AN6998/laws/LF1.pdf",
            "http://live.ecode360.com/AN6998/laws/LF1.pdf",
        ],
    )
    async def test_near_misses_are_refused_before_any_request(self, url):
        p = _attachment_plugin(_pdf_response(["x"]))
        result = await p.execute_tool("read_attachment", {"url": url})

        assert result.success is False
        assert "ordinance" in result.error_message
        assert p.requests == []

    @pytest.mark.asyncio
    async def test_result_leads_with_what_an_ordinance_pdf_is(self):
        p = _attachment_plugin(_pdf_response(["seven [nine][SEVEN] members"]))
        result = await p.execute_tool("read_attachment", {"url": LAW_URL})

        assert result.success is True, result.error_message
        sc = result.structured_content
        validate(sc, ATTACHMENT_OUTPUT_SCHEMA)
        codes = [c["code"] for c in sc["caveats"]]
        assert codes[:2] == ["ordinance_pdf", CAVEAT_PDF_TEXT_EXTRACTION]
        note = sc["caveats"][0]["message"]
        assert "not code text" in note
        assert "bracketed text as deleted" in note
        assert LAW_URL in note
        text = result.content[0]["text"]
        for caveat in sc["caveats"]:
            assert caveat["message"] in text
        # No API credentials go to the public site.
        sent = {k.lower() for k in p.requests[0].headers}
        assert "api-key" not in sent and "api-secret" not in sent

    @pytest.mark.asyncio
    async def test_a_table_attachment_does_not_get_the_ordinance_note(self):
        p = _attachment_plugin(_pdf_response(["x"]))
        sc = (
            await p.execute_tool("read_attachment", {"url": GOOD_URL})
        ).structured_content
        assert "ordinance_pdf" not in [c["code"] for c in sc["caveats"]]
