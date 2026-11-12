"""Section-number resolution, transient-failure retry, and static caching.

Three behaviours added together in 1.2.0:

* get_section accepts a section NUMBER ("21.05.010") and resolves it to a
  guid by EXACT match on a search hit's number -- never a fuzzy pick.
* Upstream connection errors and 5xx are retried inside one per-call time
  budget; timeouts are never retried, and no failure surfaces as an empty
  message (every httpx timeout stringifies to '').
* The table of contents and customer record are cached per instance.
"""

import time

import httpx
import pytest
from unittest.mock import AsyncMock, MagicMock

import plugins.ecode.plugin as ecode_mod
from plugins.ecode.config_schema import EcodePluginConfig
from plugins.ecode.plugin import (
    CAVEAT_RESOLVED_FROM_SECTION_NUMBER,
    EcodePlugin,
)

REQ = httpx.Request("GET", "https://api.ecode360.com/v1/x")


@pytest.fixture
def ecode_config():
    return {
        "enabled": True,
        "base_url": "https://api.ecode360.com",
        "customer_id": "AN6998",
        "city_name": "Municipality of Anchorage",
        "timeout": 20,
    }


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch):
    monkeypatch.setattr(ecode_mod, "RETRY_BACKOFF_S", 0.0)


def _json(body, status=200):
    return httpx.Response(status_code=status, json=body, request=REQ)


def _plugin(ecode_config, side_effect):
    p = EcodePlugin(ecode_config)
    p.plugin_config = EcodePluginConfig(**ecode_config)
    p.client = MagicMock()
    p.client.get = AsyncMock(side_effect=side_effect)
    return p


# Shape of the live search for "21.05.010" (2026-11-12): the section itself
# first, then sections that merely cite it, then an old-code PDF.
SEARCH_21_05_010 = {
    "totalResultCount": 4,
    "maxPages": 1,
    "page": "0",
    "results": [
        {"id": "49422044", "number": "§ 21.05.010",
         "title": "Table of allowed uses.", "type": "section", "pdf": False},
        {"id": "49420252", "number": "§ 21.03.040",
         "title": "Alcohol", "type": "section", "pdf": False},
        {"id": "49422044", "number": "§ 21.05.010",
         "title": "Table of allowed uses.", "type": "section", "pdf": False},
        {"id": "753304506", "number": "21.05.010",
         "title": "OLD Title 21", "type": "doc", "pdf": True},
    ],
}

CONTENT_49422044 = {
    "id": "49422044",
    "number": "§ 21.05.010",
    "title": "Table of allowed uses.",
    "type": "section",
    "content": "Table 21.05-1 below lists the uses allowed.",
    "children": [],
}


# ── Section-number resolution ───────────────────────────────────────────


class TestSectionRef:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("21.05.010", "21.05.010"),
            ("§ 21.05.010", "21.05.010"),
            ("  §21.05.010 ", "21.05.010"),
            ("CCT.020", "CCT.020"),
            ("CT-R.010", "CT-R.010"),
            ("49422044", None),
            ("ROOT", None),
            ("fence", None),
            ("..", None),
            (None, None),
            (21.05, None),
        ],
    )
    def test_recognises_section_numbers_only(self, raw, expected):
        assert EcodePlugin._section_ref(raw) == expected


class TestResolveSectionNumber:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("arg", ["21.05.010", "§ 21.05.010"])
    async def test_number_resolves_and_fetches_that_section(
        self, ecode_config, arg
    ):
        paths = []

        async def fake_get(path, params=None, **kw):
            paths.append((path, params))
            if path.endswith("/search"):
                return _json(SEARCH_21_05_010)
            return _json(CONTENT_49422044)

        p = _plugin(ecode_config, fake_get)
        result = await p.execute_tool("get_section", {"guid": arg})

        assert result.success is True, result.error_message
        assert paths[0][1] == {"query": "21.05.010", "page": 0}
        assert paths[1][0].endswith("/code/content/49422044")
        codes = [c["code"] for c in result.structured_content["caveats"]]
        assert CAVEAT_RESOLVED_FROM_SECTION_NUMBER in codes
        assert "resolved to guid 49422044" in result.content[0]["text"]
        assert result.structured_content["guid"] == "49422044"

    @pytest.mark.asyncio
    async def test_plain_guid_skips_search_and_carries_no_caveat(
        self, ecode_config
    ):
        p = _plugin(ecode_config, [_json(CONTENT_49422044)])
        result = await p.execute_tool("get_section", {"guid": "49422044"})

        assert result.success is True
        assert p.client.get.await_count == 1
        codes = [c["code"] for c in result.structured_content["caveats"]]
        assert CAVEAT_RESOLVED_FROM_SECTION_NUMBER not in codes

    @pytest.mark.asyncio
    async def test_pdf_hit_with_the_same_number_is_not_taken(
        self, ecode_config
    ):
        """Old-code PDFs carry the number too; they are not the section."""
        only_pdf = {"results": [SEARCH_21_05_010["results"][3]]}
        p = _plugin(ecode_config, [_json(only_pdf)])
        result = await p.execute_tool("get_section", {"guid": "21.05.010"})

        assert result.success is False
        assert "No section numbered '21.05.010'" in result.error_message
        assert "search_code" in result.error_message

    @pytest.mark.asyncio
    async def test_citing_sections_are_not_a_match(self, ecode_config):
        """A hit that MENTIONS the number is not the section."""
        citing = {"results": [SEARCH_21_05_010["results"][1]]}
        p = _plugin(ecode_config, [_json(citing)])
        result = await p.execute_tool("get_section", {"guid": "21.05.010"})

        assert result.success is False
        assert p.client.get.await_count == 1  # never fetched content

    @pytest.mark.asyncio
    async def test_two_different_nodes_with_the_number_is_ambiguous(
        self, ecode_config
    ):
        two = {
            "results": [
                {"id": "1", "number": "§ 1.10.010", "pdf": False},
                {"id": "2", "number": "1.10.010", "pdf": False},
            ]
        }
        p = _plugin(ecode_config, [_json(two)])
        result = await p.execute_tool("get_section", {"guid": "1.10.010"})

        assert result.success is False
        assert "2 different nodes" in result.error_message
        assert "1, 2" in result.error_message


# ── Retry and error messages ────────────────────────────────────────────


class TestRetry:
    @pytest.mark.asyncio
    async def test_connect_error_is_retried(self, ecode_config):
        p = _plugin(
            ecode_config,
            [httpx.ConnectError(""), _json(CONTENT_49422044)],
        )
        result = await p.execute_tool("get_section", {"guid": "49422044"})

        assert result.success is True
        assert p.client.get.await_count == 2

    @pytest.mark.asyncio
    async def test_5xx_is_retried(self, ecode_config):
        p = _plugin(
            ecode_config,
            [_json({"message": "busy"}, 503), _json(CONTENT_49422044)],
        )
        result = await p.execute_tool("get_section", {"guid": "49422044"})

        assert result.success is True
        assert p.client.get.await_count == 2

    @pytest.mark.asyncio
    async def test_persistent_5xx_gives_up_with_a_named_cause(
        self, ecode_config
    ):
        p = _plugin(ecode_config, [_json({"message": "busy"}, 503)] * 5)
        result = await p.execute_tool("get_section", {"guid": "49422044"})

        assert result.success is False
        assert p.client.get.await_count == ecode_mod.MAX_ATTEMPTS
        assert "HTTP 503" in result.error_message
        assert f"after {ecode_mod.MAX_ATTEMPTS} attempt(s)" in (
            result.error_message
        )

    @pytest.mark.asyncio
    async def test_persistent_connect_error_names_the_exception(
        self, ecode_config
    ):
        p = _plugin(ecode_config, [httpx.ConnectError("")] * 5)
        result = await p.execute_tool("get_section", {"guid": "49422044"})

        assert result.success is False
        assert "Could not reach eCode360" in result.error_message
        assert "ConnectError" in result.error_message

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "exc", [httpx.ReadTimeout(""), httpx.ConnectTimeout("")]
    )
    async def test_timeout_is_not_retried_and_says_so(self, ecode_config, exc):
        """The bug: str(httpx.ReadTimeout('')) == '' -> "Tool execution
        failed". A retry could not fit in the budget, so none is made."""
        p = _plugin(ecode_config, [exc, _json(CONTENT_49422044)])
        result = await p.execute_tool("get_section", {"guid": "49422044"})

        assert result.success is False
        assert p.client.get.await_count == 1
        assert "did not respond within 20s" in result.error_message
        assert type(exc).__name__ in result.error_message

    @pytest.mark.asyncio
    async def test_404_is_not_retried(self, ecode_config):
        p = _plugin(ecode_config, [_json({"message": "nope"}, 404)] * 3)
        result = await p.execute_tool("get_section", {"guid": "1"})

        assert result.success is False
        assert p.client.get.await_count == 1

    @pytest.mark.asyncio
    async def test_retries_stop_once_the_call_is_old(
        self, ecode_config, monkeypatch
    ):
        monkeypatch.setattr(ecode_mod, "RETRY_ONLY_WITHIN_S", -1.0)
        p = _plugin(ecode_config, [httpx.ConnectError("")] * 3)
        result = await p.execute_tool("get_section", {"guid": "1"})

        assert result.success is False
        assert p.client.get.await_count == 1

    @pytest.mark.asyncio
    async def test_non_json_200_is_named(self, ecode_config):
        html = httpx.Response(
            status_code=200,
            content=b"<html>maintenance</html>",
            headers={"content-type": "text/html"},
            request=REQ,
        )
        p = _plugin(ecode_config, [html])
        result = await p.execute_tool("get_section", {"guid": "1"})

        assert result.success is False
        assert "non-JSON" in result.error_message
        assert "text/html" in result.error_message

    @pytest.mark.asyncio
    async def test_empty_exception_never_yields_a_bare_failure(
        self, ecode_config
    ):
        p = _plugin(ecode_config, [KeyError()])
        result = await p.execute_tool("get_section", {"guid": "1"})

        assert result.success is False
        assert result.error_message == "Tool execution failed (KeyError)"


class TestCallBudget:
    @pytest.mark.asyncio
    async def test_no_timeout_override_on_a_fresh_call(self, ecode_config):
        p = _plugin(ecode_config, [_json(CONTENT_49422044)])
        await p.execute_tool("get_section", {"guid": "1"})

        assert "timeout" not in p.client.get.await_args.kwargs

    @pytest.mark.asyncio
    async def test_later_request_gets_only_the_remaining_budget(
        self, ecode_config, monkeypatch
    ):
        """Search + fetch must not add up past the Lambda timeout."""
        monkeypatch.setattr(ecode_mod, "TOOL_CALL_BUDGET_S", 5.0)
        p = _plugin(
            ecode_config,
            [_json(SEARCH_21_05_010), _json(CONTENT_49422044)],
        )
        await p.execute_tool("get_section", {"guid": "21.05.010"})

        for call in p.client.get.await_args_list:
            assert 0 < call.kwargs["timeout"] <= 5.0

    @pytest.mark.asyncio
    async def test_exhausted_budget_fails_before_calling_upstream(
        self, ecode_config, monkeypatch
    ):
        monkeypatch.setattr(ecode_mod, "TOOL_CALL_BUDGET_S", -1.0)
        p = _plugin(ecode_config, [_json(CONTENT_49422044)])
        result = await p.execute_tool("get_section", {"guid": "1"})

        assert result.success is False
        assert "budget" in result.error_message
        assert p.client.get.await_count == 0

    @pytest.mark.asyncio
    async def test_budget_does_not_leak_outside_a_call(self, ecode_config):
        p = _plugin(ecode_config, [_json({})])
        await p.execute_tool("get_customer_info", {})
        assert ecode_mod._call_deadline.get() is None

    def test_direct_helper_use_has_no_budget(self, ecode_config):
        p = EcodePlugin(ecode_config)
        p.plugin_config = EcodePluginConfig(**ecode_config)
        assert p._request_timeout() is None


# ── Static caching ──────────────────────────────────────────────────────


class TestStaticCache:
    @pytest.mark.asyncio
    async def test_toc_is_fetched_once(self, ecode_config):
        toc = {"guid": "ROOT", "title": "Code", "children": []}
        p = _plugin(ecode_config, [_json(toc)])
        for _ in range(3):
            result = await p.execute_tool("get_table_of_contents", {})
            assert result.success is True
        assert p.client.get.await_count == 1

    @pytest.mark.asyncio
    async def test_customer_info_is_fetched_once(self, ecode_config):
        cust = {"results": {"id": "AN6998", "name": "Anchorage"}}
        p = _plugin(ecode_config, [_json(cust)])
        for _ in range(2):
            result = await p.execute_tool("get_customer_info", {})
            assert "AN6998" in result.content[0]["text"]
        assert p.client.get.await_count == 1

    @pytest.mark.asyncio
    async def test_failures_are_not_cached(self, ecode_config):
        toc = {"guid": "ROOT", "title": "Code", "children": []}
        p = _plugin(
            ecode_config, [_json({"message": "x"}, 500)] * 3 + [_json(toc)]
        )
        first = await p.execute_tool("get_table_of_contents", {})
        second = await p.execute_tool("get_table_of_contents", {})

        assert first.success is False
        assert second.success is True

    @pytest.mark.asyncio
    async def test_expired_entry_is_refetched(self, ecode_config):
        toc = {"guid": "ROOT", "title": "Code", "children": []}
        p = _plugin(ecode_config, [_json(toc), _json(toc)])
        await p.execute_tool("get_table_of_contents", {})
        for path, (_exp, data) in list(p._static_cache.items()):
            p._static_cache[path] = (time.monotonic() - 1, data)
        await p.execute_tool("get_table_of_contents", {})

        assert p.client.get.await_count == 2

    @pytest.mark.asyncio
    async def test_cache_is_per_instance(self, ecode_config):
        toc = {"guid": "ROOT", "title": "Code", "children": []}
        a = _plugin(ecode_config, [_json(toc)])
        b = _plugin(ecode_config, [_json(toc)])
        await a.execute_tool("get_table_of_contents", {})
        await b.execute_tool("get_table_of_contents", {})

        assert a.client.get.await_count == 1
        assert b.client.get.await_count == 1

    @pytest.mark.asyncio
    async def test_fallback_marker_does_not_pollute_the_cache(
        self, ecode_config
    ):
        root = {"guid": "ROOT", "title": "Code", "children": []}
        p = _plugin(
            ecode_config,
            [_json({"message": "nf"}, 404), _json(root)],
        )
        data = await p._get_toc()

        assert data["_fallback"]
        cached = [d for _e, d in p._static_cache.values()]
        assert all("_fallback" not in d for d in cached)

