"""read_attachment(find=...): locate a phrase in a long PDF by page.

One attached document runs to several hundred pages and a call returns
about a dozen, so a search hit inside it could not be reached by paging.
`find` lists the pages a phrase is on. The text of every page is kept (in
the container and, when configured, the shared cache bucket) because
extracting it takes longer than one call may spend; a call that runs out
of time says how far it got and the next one continues from there.
"""

import json
import zlib

import httpx
import pytest
from jsonschema import validate

import plugins.ecode.plugin as ecode_mod
from plugins.ecode.plugin import (
    ATTACHMENT_OUTPUT_SCHEMA,
    CAVEAT_FIND_INCOMPLETE,
    CAVEAT_FIND_MORE_PAGES,
    CAVEAT_FIND_SNIPPETS,
    CAVEAT_FIND_WORDS_NOT_PHRASE,
    CAVEAT_MORE_ATTACHMENT_PAGES,
    CAVEAT_NO_TEXT_LAYER,
    CAVEAT_ORDINANCE_PDF,
    FIND_MAX_CHARS,
    FIND_PAGES_LISTED,
    EcodePlugin,
)
from tests.test_ecode_chapters_and_attachments import (
    GOOD_URL,
    _attachment_plugin,
    _pdf_response,
    make_pdf,
)
from tests.test_ecode_shared_cache import FakeS3

CODE_DATE = "2026-11-10"
ORDINANCE_URL = "https://live.ecode360.com/AN6998/laws/LF1234567.pdf"

PAGES = [
    "General provisions and purpose",
    "A kennel is a conditional use in this district",
    "Setbacks for a kennel: see the Conditional Use standards",
    "Parking. Use of a lot is conditional on access",
]


@pytest.fixture(autouse=True)
def clear_caches():
    ecode_mod._ATTACHMENT_CACHE.clear()
    ecode_mod._ATTACHMENT_TEXT_CACHE.clear()
    yield
    ecode_mod._ATTACHMENT_CACHE.clear()
    ecode_mod._ATTACHMENT_TEXT_CACHE.clear()


@pytest.fixture
def s3(monkeypatch):
    fake = FakeS3()

    async def dated(self):
        return fake.code_date

    fake.code_date = CODE_DATE
    monkeypatch.setattr(ecode_mod, "ATTACHMENT_TEXT_SHARED_MIN_PAGES", 1)
    monkeypatch.setattr(EcodePlugin, "_code_last_updated", dated)
    monkeypatch.setattr(
        EcodePlugin, "_shared_cache_client", staticmethod(lambda: (fake, "bkt"))
    )
    return fake


@pytest.fixture
def one_page_per_call(monkeypatch):
    """Every call's time runs out after it has extracted one more page."""
    monkeypatch.setattr(ecode_mod, "FIND_TIME_RESERVE_S", 10_000.0)


def new_container():
    ecode_mod._ATTACHMENT_CACHE.clear()
    ecode_mod._ATTACHMENT_TEXT_CACHE.clear()


async def _find(plugin, find, url=GOOD_URL, **args):
    result = await plugin.execute_tool(
        "read_attachment", {"url": url, "find": find, **args}
    )
    assert result.success, result.error_message
    sc = result.structured_content
    validate(sc, ATTACHMENT_OUTPUT_SCHEMA)
    text = result.content[0]["text"]
    for caveat in sc["caveats"]:
        assert caveat["message"] in text
    assert sc["pages"] == []
    return sc, text


def _codes(sc):
    return [c["code"] for c in sc["caveats"]]


class TestFind:
    @pytest.mark.asyncio
    async def test_lists_the_pages_a_phrase_is_on(self):
        plugin = _attachment_plugin(_pdf_response(PAGES))
        sc, text = await _find(plugin, "conditional use")

        assert [m["page"] for m in sc["find"]["matches"]] == [2, 3]
        assert sc["find"]["match"] == "phrase"
        assert sc["find"]["pages_matched"] == 2
        assert (sc["find"]["searched_from"], sc["find"]["searched_to"]) == (1, 4)
        assert sc["page_count"] == 4
        assert sc["next_page"] is None
        assert "occurs 2 time(s) on 2 page(s)" in sc["summary"]
        assert "--- page 2 of 4 (1 match) ---" in text
        assert "A kennel is a conditional use in this district" in text
        assert _codes(sc) == [CAVEAT_FIND_SNIPPETS]

    @pytest.mark.asyncio
    async def test_case_and_spacing_do_not_matter(self):
        plugin = _attachment_plugin(_pdf_response(PAGES))
        sc, _ = await _find(plugin, "  CONDITIONAL   use ")
        assert sc["find"]["query"] == "CONDITIONAL use"
        assert [m["page"] for m in sc["find"]["matches"]] == [2, 3]

    @pytest.mark.asyncio
    async def test_every_occurrence_on_a_page_is_counted(self):
        plugin = _attachment_plugin(
            _pdf_response(["kennel, kennel and kennel", "none here"])
        )
        sc, text = await _find(plugin, "kennel")
        assert sc["find"]["matches"][0]["count"] == 3
        assert "(3 matches)" in text

    @pytest.mark.asyncio
    async def test_a_phrase_that_is_not_there_is_said_plainly(self):
        plugin = _attachment_plugin(_pdf_response(PAGES))
        sc, _ = await _find(plugin, "helicopter")
        assert sc["find"]["matches"] == []
        assert "was not found in pages 1-4 of 4" in sc["summary"]
        assert sc["caveats"] == []

    @pytest.mark.asyncio
    async def test_words_apart_are_offered_but_not_as_a_match(self):
        plugin = _attachment_plugin(_pdf_response(PAGES))
        sc, _ = await _find(plugin, "conditional access")
        assert sc["find"]["match"] == "all_words"
        assert [m["page"] for m in sc["find"]["matches"]] == [4]
        assert CAVEAT_FIND_WORDS_NOT_PHRASE in _codes(sc)
        assert "was not found" in sc["summary"]

    @pytest.mark.asyncio
    async def test_searching_starts_at_page(self):
        plugin = _attachment_plugin(_pdf_response(PAGES))
        sc, _ = await _find(plugin, "conditional use", page=3)
        assert [m["page"] for m in sc["find"]["matches"]] == [3]
        assert sc["find"]["searched_from"] == 3

    @pytest.mark.asyncio
    async def test_many_matching_pages_are_listed_in_parts(self):
        pages = ["kennel"] * (FIND_PAGES_LISTED + 5)
        plugin = _attachment_plugin(_pdf_response(pages))
        sc, _ = await _find(plugin, "kennel")
        assert len(sc["find"]["matches"]) == FIND_PAGES_LISTED
        assert sc["find"]["pages_matched"] == FIND_PAGES_LISTED + 5
        assert sc["next_page"] == FIND_PAGES_LISTED + 1
        assert CAVEAT_FIND_MORE_PAGES in _codes(sc)

        sc, _ = await _find(plugin, "kennel", page=sc["next_page"])
        assert len(sc["find"]["matches"]) == 5
        assert sc["next_page"] is None

    @pytest.mark.asyncio
    async def test_pages_without_text_are_flagged(self):
        plugin = _attachment_plugin(_pdf_response(["", ""]))
        sc, _ = await _find(plugin, "kennel")
        assert CAVEAT_NO_TEXT_LAYER in _codes(sc)

    @pytest.mark.asyncio
    async def test_an_ordinance_pdf_is_still_called_one(self):
        plugin = _attachment_plugin(_pdf_response(PAGES))
        sc, _ = await _find(plugin, "kennel", url=ORDINANCE_URL)
        assert _codes(sc)[0] == CAVEAT_ORDINANCE_PDF

    @pytest.mark.asyncio
    async def test_the_document_is_downloaded_and_extracted_once(
        self, monkeypatch
    ):
        plugin = _attachment_plugin(_pdf_response(PAGES))
        extractions = []
        real = EcodePlugin._extract_page_texts

        def counted(*args):
            extractions.append(1)
            return real(*args)

        monkeypatch.setattr(
            EcodePlugin, "_extract_page_texts", staticmethod(counted)
        )
        await _find(plugin, "kennel")
        await _find(plugin, "parking")
        assert len(plugin.requests) == 1
        assert len(extractions) == 1


class TestArguments:
    @pytest.mark.parametrize(
        "args,needle",
        [
            ({"find": 7}, "find must be a string"),
            ({"find": "x" * (FIND_MAX_CHARS + 1)}, "the limit is"),
            ({"find": "kennel", "page": 9}, "past the end"),
            ({"find": "kennel", "page": 0}, "page must be 1 or greater"),
        ],
    )
    @pytest.mark.asyncio
    async def test_bad_arguments_are_caller_errors(self, args, needle):
        plugin = _attachment_plugin(_pdf_response(PAGES))
        result = await plugin.execute_tool(
            "read_attachment", {"url": GOOD_URL, **args}
        )
        assert result.success is False
        assert needle in result.error_message

    @pytest.mark.asyncio
    async def test_a_blank_find_reads_pages_as_before(self):
        plugin = _attachment_plugin(_pdf_response(PAGES))
        result = await plugin.execute_tool(
            "read_attachment", {"url": GOOD_URL, "find": "  "}
        )
        assert result.success
        assert "find" not in result.structured_content
        assert len(result.structured_content["pages"]) == 4

    def test_the_tool_declares_find(self):
        plugin = _attachment_plugin(_pdf_response(PAGES))
        tool = next(t for t in plugin.get_tools() if t.name == "read_attachment")
        assert tool.input_schema["properties"]["find"]["maxLength"] == (
            FIND_MAX_CHARS
        )
        assert tool.input_schema["required"] == ["url"]
        assert "find" in tool.description

    @pytest.mark.asyncio
    async def test_a_page_read_that_stops_early_mentions_find(
        self, monkeypatch
    ):
        monkeypatch.setattr(ecode_mod, "ATTACHMENT_TEXT_CHARS", 40)
        plugin = _attachment_plugin(_pdf_response(PAGES))
        result = await plugin.execute_tool(
            "read_attachment", {"url": GOOD_URL}
        )
        (caveat,) = [
            c for c in result.structured_content["caveats"]
            if c["code"] == CAVEAT_MORE_ATTACHMENT_PAGES
        ]
        assert "pass find" in caveat["message"]


class TestOutOfTime:
    @pytest.mark.asyncio
    async def test_a_call_says_how_far_it_searched(self, one_page_per_call):
        plugin = _attachment_plugin(_pdf_response(PAGES))
        sc, _ = await _find(plugin, "a kennel")
        assert sc["find"]["searched_to"] == 1
        assert sc["find"]["matches"] == []
        assert sc["next_page"] == 2
        (caveat,) = sc["caveats"]
        assert caveat["code"] == CAVEAT_FIND_INCOMPLETE
        assert caveat["message"].startswith("INCOMPLETE")
        assert "page=2" in caveat["message"]

    @pytest.mark.asyncio
    async def test_following_next_page_searches_every_page_once(
        self, one_page_per_call
    ):
        plugin = _attachment_plugin(_pdf_response(PAGES))
        found, searched, page = [], [], 1
        for _ in range(10):
            sc, _ = await _find(plugin, "a kennel", page=page)
            found += [m["page"] for m in sc["find"]["matches"]]
            searched.append(
                (sc["find"]["searched_from"], sc["find"]["searched_to"])
            )
            if sc["next_page"] is None:
                break
            page = sc["next_page"]
        else:
            pytest.fail("never reached the end of the document")
        assert found == [2, 3]
        assert searched == [(1, 1), (2, 2), (3, 3), (4, 4)]
        assert len(plugin.requests) == 1

    @pytest.mark.asyncio
    async def test_a_start_page_not_reached_yet_is_not_called_no_match(
        self, one_page_per_call
    ):
        plugin = _attachment_plugin(_pdf_response(PAGES))
        sc, _ = await _find(plugin, "a kennel", page=3)
        assert sc["find"]["searched_from"] is None
        assert sc["next_page"] == 3
        assert sc["caveats"][0]["message"].startswith("NOT SEARCHED YET")
        assert "not found" not in sc["summary"]

        sc, _ = await _find(plugin, "a kennel", page=3)  # pages 1-2
        sc, _ = await _find(plugin, "a kennel", page=3)
        assert [m["page"] for m in sc["find"]["matches"]] == [3]


    @pytest.mark.asyncio
    async def test_a_long_list_from_part_of_the_document_says_so(
        self, monkeypatch
    ):
        monkeypatch.setattr(ecode_mod, "FIND_PAGES_LISTED", 1)
        calls = iter([2, 99])

        real = EcodePlugin._extract_page_texts

        def two_pages(data, done, _deadline):
            count, texts = real(data, done, None)
            return count, texts[: len(done) + next(calls)]

        monkeypatch.setattr(
            EcodePlugin, "_extract_page_texts", staticmethod(two_pages)
        )
        plugin = _attachment_plugin(_pdf_response(["kennel"] * 4))
        sc, _ = await _find(plugin, "kennel")
        assert _codes(sc)[:2] == [CAVEAT_FIND_MORE_PAGES, CAVEAT_FIND_INCOMPLETE]
        assert sc["next_page"] == 2
        sc, _ = await _find(plugin, "kennel", page=2)
        assert sc["find"]["searched_to"] == 4
        assert CAVEAT_FIND_INCOMPLETE not in _codes(sc)


class TestSharedCache:
    @pytest.mark.asyncio
    async def test_another_container_does_not_download_the_pdf(self, s3):
        first = _attachment_plugin(_pdf_response(PAGES))
        await _find(first, "kennel")
        ((_, key, content_type),) = s3.puts
        assert key.startswith("v1/attachment-text/")
        assert content_type == "application/gzip"
        stored = json.loads(zlib.decompress(s3.objects[key], 31))
        assert stored["url"] == GOOD_URL.replace(" ", "%20")
        assert stored["page_count"] == 4
        assert stored["texts"][1] == PAGES[1]

        new_container()
        second = _attachment_plugin(_pdf_response(PAGES))
        sc, _ = await _find(second, "conditional use")
        assert [m["page"] for m in sc["find"]["matches"]] == [2, 3]
        assert second.requests == []
        assert len(s3.puts) == 1

    @pytest.mark.asyncio
    async def test_unfinished_text_is_continued_by_another_container(
        self, s3, one_page_per_call
    ):
        page = 1
        for _ in range(4):
            new_container()
            plugin = _attachment_plugin(_pdf_response(PAGES))
            sc, _ = await _find(plugin, "access", page=page)
            page = sc["next_page"]
        assert page is None
        assert [m["page"] for m in sc["find"]["matches"]] == [4]
        assert len(s3.puts) == 4

    @pytest.mark.asyncio
    async def test_text_stored_for_an_earlier_code_date_is_rebuilt(self, s3):
        await _find(_attachment_plugin(_pdf_response(PAGES)), "kennel")
        new_container()
        s3.code_date = "2027-01-05"
        plugin = _attachment_plugin(_pdf_response(PAGES))
        await _find(plugin, "kennel")
        assert len(plugin.requests) == 1
        assert len(s3.puts) == 2

    @pytest.mark.parametrize(
        "body",
        [
            b"not gzip",
            zlib.compress(b"[1, 2]", wbits=31),
            zlib.compress(
                json.dumps(
                    {
                        "version": 1,
                        "url": "https://live.ecode360.com/attachment/1/b.pdf",
                        "code_last_updated": CODE_DATE,
                        "fetched_at": 0,
                        "page_count": 1,
                        "texts": ["other document"],
                    }
                ).encode(),
                wbits=31,
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_an_unusable_object_is_ignored(self, s3, body):
        key = EcodePlugin._attachment_text_key(
            GOOD_URL.replace(" ", "%20")
        )
        s3.objects[key] = body
        plugin = _attachment_plugin(_pdf_response(PAGES))
        sc, _ = await _find(plugin, "conditional use")
        assert [m["page"] for m in sc["find"]["matches"]] == [2, 3]
        assert len(plugin.requests) == 1

    @pytest.mark.asyncio
    async def test_a_short_document_is_not_stored(self, s3, monkeypatch):
        monkeypatch.setattr(ecode_mod, "ATTACHMENT_TEXT_SHARED_MIN_PAGES", 5)
        await _find(_attachment_plugin(_pdf_response(PAGES)), "kennel")
        assert s3.puts == []

    @pytest.mark.asyncio
    async def test_a_broken_bucket_never_fails_the_call(self, s3):
        s3.get_error = RuntimeError("bucket down")
        s3.put_error = RuntimeError("bucket down")
        plugin = _attachment_plugin(_pdf_response(PAGES))
        sc, _ = await _find(plugin, "conditional use")
        assert [m["page"] for m in sc["find"]["matches"]] == [2, 3]

    @pytest.mark.asyncio
    async def test_without_the_code_date_the_bucket_is_left_alone(self, s3):
        s3.code_date = None
        plugin = _attachment_plugin(_pdf_response(PAGES))
        sc, _ = await _find(plugin, "conditional use")
        assert [m["page"] for m in sc["find"]["matches"]] == [2, 3]
        assert s3.gets == [] and s3.puts == []


def test_a_corrupt_pdf_is_a_readable_error():
    with pytest.raises(ecode_mod.EcodeAPIError, match="could not be opened"):
        EcodePlugin._extract_page_texts(b"%PDF-1.4 nonsense", [], None)


def test_make_pdf_pages_read_back():
    # The fixture these tests stand on: one line of text per page.
    count, texts = EcodePlugin._extract_page_texts(make_pdf(PAGES), [], None)
    assert (count, texts) == (4, PAGES)
    assert httpx  # the transport the fixture plugin is built on
