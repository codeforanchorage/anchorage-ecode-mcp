"""The short-lived cache of search pages, section text and structure nodes.

A conversation re-reads the same things in bursts (a section after a
search resolved its number, each `offset` part of a long section, a
repeated search), and every repeat used to be another request on the
licensed upstream API. Successes are kept for a few minutes per warm
container; failures never are; the cache is bounded in size.
"""

import copy
import time

import httpx
import pytest
from unittest.mock import AsyncMock, MagicMock

import plugins.ecode.plugin as ecode_mod
from plugins.ecode.config_schema import EcodePluginConfig
from plugins.ecode.plugin import EcodePlugin

REQ = httpx.Request("GET", "https://api.ecode360.com/v1/x")

SEARCH = {
    "totalResultCount": 1,
    "maxPages": 1,
    "page": "0",
    "results": [
        {
            "id": "49422044",
            "number": "21.05.010",
            "title": "Purpose",
            "text": 'the <em class="highlight">purpose</em> of this',
            "url": "https://live.ecode360.com/49422044?highlight=purpose",
            "type": "section",
        }
    ],
}
SECTION = {
    "guid": "49422044",
    "number": "21.05.010",
    "title": "Purpose",
    "content": "<p>The purpose of this chapter.</p>",
}
STRUCTURE = {
    "error": False,
    "results": {
        "guid": "49401992",
        "title": "Title 21",
        "label": "Title",
        "children": [
            {"guid": "49422044", "number": "21.05.010", "title": "Purpose"}
        ],
    },
}


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


class TestRecentCache:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "tool,args,body",
        [
            ("search_code", {"query": "purpose"}, SEARCH),
            ("get_section", {"guid": "49422044"}, SECTION),
            ("browse_structure", {"guid": "49401992"}, STRUCTURE),
        ],
    )
    async def test_repeat_is_served_without_an_upstream_call(
        self, ecode_config, tool, args, body
    ):
        """One response is supplied: a second fetch would raise."""
        pristine = copy.deepcopy(body)
        p = _plugin(ecode_config, [_json(body)])
        first = await p.execute_tool(tool, args)
        second = await p.execute_tool(tool, args)

        assert first.success is True and second.success is True
        assert p.client.get.await_count == 1
        # Same answer both times, and the shared entry was not mutated.
        assert second.content == first.content
        cached = [d for _e, d, _n in ecode_mod._RECENT_CACHE.values()]
        assert cached == [pristine]

    @pytest.mark.asyncio
    async def test_cache_survives_a_new_plugin_instance(self, ecode_config):
        """The Lambda adapter builds a new plugin per invocation."""
        a = _plugin(ecode_config, [_json(SECTION)])
        await a.execute_tool("get_section", {"guid": "49422044"})
        b = _plugin(ecode_config, [])
        result = await b.execute_tool("get_section", {"guid": "49422044"})

        assert result.success is True
        assert b.client.get.await_count == 0

    @pytest.mark.asyncio
    async def test_different_page_or_query_is_a_different_entry(
        self, ecode_config
    ):
        p = _plugin(ecode_config, [_json(SEARCH)] * 3)
        await p.execute_tool("search_code", {"query": "purpose"})
        await p.execute_tool("search_code", {"query": "purpose", "page": 1})
        await p.execute_tool("search_code", {"query": "intent"})

        assert p.client.get.await_count == 3

    @pytest.mark.asyncio
    async def test_section_by_number_is_free_the_second_time(
        self, ecode_config
    ):
        """Reading by number costs a search and a fetch; reading the same
        section again, by number or by guid, costs nothing."""
        p = _plugin(ecode_config, [_json(SEARCH), _json(SECTION)])
        first = await p.execute_tool("get_section", {"guid": "21.05.010"})
        again = await p.execute_tool("get_section", {"guid": "21.05.010"})
        by_guid = await p.execute_tool("get_section", {"guid": "49422044"})

        assert first.success and again.success and by_guid.success
        assert p.client.get.await_count == 2

    @pytest.mark.asyncio
    async def test_offset_parts_fetch_the_node_once(self, ecode_config):
        """Each part of a long section used to re-fetch the whole node."""
        long_text = "\n".join(
            f"<p>Line {i} of the section, padded out to some length.</p>"
            for i in range(3000)
        )
        node = dict(SECTION, content=long_text)
        p = _plugin(ecode_config, [_json(node)])
        part1 = await p.execute_tool("get_section", {"guid": "49422044"})
        part2 = await p.execute_tool(
            "get_section",
            {"guid": "49422044", "offset": ecode_mod.MAX_CONTENT_CHARS},
        )

        assert part1.success is True and part2.success is True
        assert p.client.get.await_count == 1

    @pytest.mark.asyncio
    async def test_failures_are_not_cached(self, ecode_config):
        p = _plugin(
            ecode_config,
            [_json({"message": "x"}, 500)] * 3 + [_json(SECTION)],
        )
        first = await p.execute_tool("get_section", {"guid": "49422044"})
        second = await p.execute_tool("get_section", {"guid": "49422044"})

        assert first.success is False
        assert second.success is True

    @pytest.mark.asyncio
    async def test_not_found_is_not_cached(self, ecode_config):
        p = _plugin(
            ecode_config, [_json({"message": "no"}, 404), _json(SECTION)]
        )
        first = await p.execute_tool("get_section", {"guid": "49422044"})
        second = await p.execute_tool("get_section", {"guid": "49422044"})

        assert first.success is False
        assert second.success is True

    @pytest.mark.asyncio
    async def test_expired_entry_is_refetched(self, ecode_config):
        p = _plugin(ecode_config, [_json(SECTION), _json(SECTION)])
        await p.execute_tool("get_section", {"guid": "49422044"})
        for key, (_exp, data, n) in list(ecode_mod._RECENT_CACHE.items()):
            ecode_mod._RECENT_CACHE[key] = (time.monotonic() - 1, data, n)
        await p.execute_tool("get_section", {"guid": "49422044"})

        assert p.client.get.await_count == 2
        assert len(ecode_mod._RECENT_CACHE) == 1

    @pytest.mark.asyncio
    async def test_a_large_node_is_not_kept(self, ecode_config, monkeypatch):
        """A whole title is megabytes; it must not sit in memory."""
        monkeypatch.setattr(ecode_mod, "RECENT_CACHE_ENTRY_CHARS", 50)
        p = _plugin(ecode_config, [_json(SECTION), _json(SECTION)])
        await p.execute_tool("get_section", {"guid": "49422044"})
        await p.execute_tool("get_section", {"guid": "49422044"})

        assert p.client.get.await_count == 2
        assert ecode_mod._RECENT_CACHE == {}

    @pytest.mark.asyncio
    async def test_oldest_entries_go_when_the_total_is_reached(
        self, ecode_config, monkeypatch
    ):
        one = len(ecode_mod.json.dumps(SECTION, ensure_ascii=False))
        # Room for two entries, not three.
        monkeypatch.setattr(
            ecode_mod, "RECENT_CACHE_TOTAL_CHARS", one * 2 + 1
        )
        p = _plugin(ecode_config, [_json(SECTION)] * 3)
        for guid in ("1001", "1002", "1003"):
            await p.execute_tool("get_section", {"guid": guid})

        paths = [key[1] for key in ecode_mod._RECENT_CACHE]
        assert len(paths) == 2
        assert paths[0].endswith("/1002") and paths[1].endswith("/1003")
        total = sum(n for _e, _d, n in ecode_mod._RECENT_CACHE.values())
        assert total <= ecode_mod.RECENT_CACHE_TOTAL_CHARS

    def test_ttl_is_short(self):
        """Nothing announces a republish, so staleness is bounded by the
        TTL alone. Keep it to minutes."""
        assert 0 < ecode_mod.RECENT_CACHE_TTL_S <= 600
