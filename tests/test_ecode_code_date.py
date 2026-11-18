"""The code's last-updated date on every section read.

get_section and get_sections report the date the published code as a whole
was last updated, read from the library record, so a reader can tell how
current the text is without asking. The lookup decorates a read and must
never fail one.
"""

import httpx
import jsonschema
import pytest
from unittest.mock import AsyncMock, MagicMock

import plugins.ecode.plugin as ecode_mod
from plugins.ecode.config_schema import EcodePluginConfig
from plugins.ecode.plugin import (
    SECTION_OUTPUT_SCHEMA,
    SECTIONS_OUTPUT_SCHEMA,
    EcodePlugin,
)

pytestmark = pytest.mark.code_date

REQ = httpx.Request("GET", "https://api.ecode360.com/v1/x")

CUSTOMER = {
    "error": False,
    "results": {
        "id": "AN6998",
        "name": "Municipality of Anchorage, AK",
        "updatedDate": "2026-11-10",
    },
}
SECTION = {
    "guid": "49422044",
    "number": "21.05.010",
    "title": "Purpose",
    "content": "<p>The purpose of this chapter.</p>",
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


def _plugin(ecode_config, customer, section=SECTION):
    """A plugin whose client answers by path, so the two concurrent
    requests of a section read can arrive in either order.

    `customer` is a response, or a list of responses used in turn.
    """
    customer_responses = (
        list(customer) if isinstance(customer, list) else None
    )

    async def get(path, **_kwargs):
        if "/code/content/" in path:
            return _json(section)
        if customer_responses is not None:
            return customer_responses.pop(0)
        return customer

    p = EcodePlugin(ecode_config)
    p.plugin_config = EcodePluginConfig(**ecode_config)
    p.client = MagicMock()
    p.client.get = AsyncMock(side_effect=get)
    return p


class TestCodeLastUpdated:
    async def test_get_section_reports_the_date(self, ecode_config):
        p = _plugin(ecode_config, _json(CUSTOMER))
        result = await p.execute_tool("get_section", {"guid": "49422044"})

        assert result.success is True
        assert result.structured_content["code_last_updated"] == "2026-11-10"
        text = result.content[0]["text"]
        assert "Published code last updated 2026-11-10" in text
        # It is the whole code's date; the wording must say so.
        assert "not this section" in text
        jsonschema.validate(result.structured_content, SECTION_OUTPUT_SCHEMA)

    async def test_get_sections_reports_the_date_once(self, ecode_config):
        p = _plugin(ecode_config, _json(CUSTOMER))
        result = await p.execute_tool(
            "get_sections", {"sections": ["49422044", "49422045"]}
        )

        assert result.success is True
        assert result.structured_content["code_last_updated"] == "2026-11-10"
        text = result.content[0]["text"]
        assert text.count("Published code last updated") == 1
        jsonschema.validate(
            result.structured_content, SECTIONS_OUTPUT_SCHEMA
        )

    async def test_library_record_is_fetched_once(self, ecode_config):
        """The record is cached, so later reads cost no extra request."""
        p = _plugin(ecode_config, _json(CUSTOMER))
        await p.execute_tool("get_section", {"guid": "49422044"})
        await p.execute_tool("get_section", {"guid": "49422045"})

        paths = [call.args[0] for call in p.client.get.await_args_list]
        assert sum("/code/content/" not in path for path in paths) == 1

    @pytest.mark.parametrize("status", [404, 500, 403])
    async def test_a_failed_lookup_does_not_fail_the_read(
        self, ecode_config, status
    ):
        p = _plugin(ecode_config, _json({"message": "x"}, status))
        result = await p.execute_tool("get_section", {"guid": "49422044"})

        assert result.success is True
        assert result.structured_content["code_last_updated"] is None
        text = result.content[0]["text"]
        assert "The purpose of this chapter." in text
        assert "Published code last updated" not in text
        jsonschema.validate(result.structured_content, SECTION_OUTPUT_SCHEMA)

    async def test_a_failed_lookup_is_retried_on_the_next_read(
        self, ecode_config
    ):
        """Failures are not cached, so the date comes back by itself."""
        p = _plugin(
            ecode_config,
            [_json({"message": "x"}, 404), _json(CUSTOMER)],
        )
        first = await p.execute_tool("get_section", {"guid": "49422044"})
        second = await p.execute_tool("get_section", {"guid": "49422044"})

        assert first.structured_content["code_last_updated"] is None
        assert second.structured_content["code_last_updated"] == "2026-11-10"

    @pytest.mark.parametrize(
        "record", [{"error": False, "results": {"updatedDate": ""}}, {}, []]
    )
    async def test_a_record_without_a_date_reports_null(
        self, ecode_config, record
    ):
        p = _plugin(ecode_config, _json(record))
        result = await p.execute_tool("get_section", {"guid": "49422044"})

        assert result.success is True
        assert result.structured_content["code_last_updated"] is None

    async def test_a_failed_section_still_fails(self, ecode_config):
        """The date lookup succeeding must not mask a section that
        could not be read."""

        async def get(path, **_kwargs):
            if "/code/content/" in path:
                return _json({"message": "no"}, 404)
            return _json(CUSTOMER)

        p = EcodePlugin(ecode_config)
        p.plugin_config = EcodePluginConfig(**ecode_config)
        p.client = MagicMock()
        p.client.get = AsyncMock(side_effect=get)
        result = await p.execute_tool("get_section", {"guid": "49422044"})

        assert result.success is False
