"""eCode360 refusing the server's credentials (HTTP 401 / 403 with JSON).

It is either a wrong key or the publisher's gateway refusing a good one
for a while. The caller can fix neither, so the error says what to do
rather than naming environment variables; the request is tried once more;
and the log line carries a fixed phrase that an alarm counts, because a
tool error is an HTTP 200 and no other alarm sees it.
"""

import logging
import pathlib
import re
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

import plugins.ecode.plugin as ecode_mod
from plugins.ecode.config_schema import EcodePluginConfig
from plugins.ecode.plugin import UPSTREAM_AUTH_LOG, EcodePlugin

REQ = httpx.Request("GET", "https://api.ecode360.com/v1/x")
DENY = {"Message": "User is not authorized to access this resource"}
CONFIG = {
    "enabled": True,
    "base_url": "https://api.ecode360.com",
    "customer_id": "AN6998",
    "city_name": "Municipality of Anchorage",
    "timeout": 20,
}
ALARMS = (
    pathlib.Path(__file__).parent.parent
    / "terraform" / "aws" / "cloudwatch_alarms.tf"
).read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch):
    monkeypatch.setattr(ecode_mod, "RETRY_BACKOFF_S", 0.0)


def _plugin(responses):
    """A plugin whose upstream answers with `responses`, in order, then
    keeps giving the last one."""
    queue = list(responses)

    async def fake_get(path, params=None, **_kw):
        status, body = queue.pop(0) if len(queue) > 1 else queue[0]
        return httpx.Response(status, json=body, request=REQ)

    p = EcodePlugin(CONFIG)
    p.plugin_config = EcodePluginConfig(**CONFIG)
    p.client = MagicMock()
    p.client.get = AsyncMock(side_effect=fake_get)
    return p


def _block(kind, name):
    found = re.search(
        r'resource "%s" "%s" \{.*?\n\}' % (kind, name), ALARMS, re.S
    )
    assert found, f"{kind}.{name} is not defined"
    return found.group(0)


class TestRefusedCredentials:
    @pytest.mark.parametrize("status", [401, 403])
    @pytest.mark.asyncio
    async def test_the_caller_is_told_what_to_do(self, status):
        plugin = _plugin([(status, DENY)])
        result = await plugin.execute_tool("search_code", {"query": "snow"})
        assert result.success is False
        message = result.error_message
        assert f"(HTTP {status})" in message
        assert "try again in a few minutes" in message
        assert "Do not answer from memory" in message
        assert "https://ecode360.com/AN6998" in message
        # The setup hint is addressed to the operator, after the advice,
        # and the publisher's own wording stays in the log.
        assert message.index("ECODE_API_KEY") > message.index("For the operator")
        assert "configuration problem" not in message
        assert "not authorized" not in message

    @pytest.mark.asyncio
    async def test_it_is_tried_once_more(self):
        plugin = _plugin([(403, DENY)])
        await plugin.execute_tool("search_code", {"query": "snow"})
        assert plugin.client.get.await_count == 2

    @pytest.mark.asyncio
    async def test_a_refusal_that_clears_is_not_an_error(self):
        plugin = _plugin([(403, DENY), (200, {"results": []})])
        result = await plugin.execute_tool("search_code", {"query": "snow"})
        assert result.success, result.error_message

    @pytest.mark.asyncio
    async def test_the_log_keeps_the_detail_under_the_alarm_phrase(
        self, caplog
    ):
        plugin = _plugin([(403, DENY)])
        with caplog.at_level(logging.ERROR, logger=ecode_mod.logger.name):
            await plugin.execute_tool("search_code", {"query": "snow"})
        (record,) = [
            r for r in caplog.records
            if r.getMessage().startswith(UPSTREAM_AUTH_LOG)
        ]
        assert "not authorized" in record.getMessage()
        assert "ECODE_API_KEY" in record.getMessage()


class TestAlarms:
    def test_a_metric_filter_counts_the_logged_phrase(self):
        block = _block("aws_cloudwatch_log_metric_filter", "upstream_auth")
        assert f'pattern        = "\\"{UPSTREAM_AUTH_LOG}\\""' in block

    def test_tool_errors_are_counted_too(self):
        block = _block("aws_cloudwatch_log_metric_filter", "tool_errors")
        assert "Error executing tool" in block

    @pytest.mark.parametrize("name", ["upstream_auth", "tool_errors"])
    def test_each_filter_has_an_alarm_that_notifies(self, name):
        block = _block("aws_cloudwatch_metric_alarm", name)
        assert "alarm_actions = local.alarm_actions" in block
        assert 'treat_missing_data  = "notBreaching"' in block
