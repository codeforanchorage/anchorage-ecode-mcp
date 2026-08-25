"""Shared pytest fixtures.

The MCP server keeps its PluginManager and MCPServer as module-level
globals in ``server.http_handler`` so a warm Lambda container can reuse
them across invocations. That is right for production and hostile to
tests: anything a test assigns to those names outlives the test and is
visible to every test that runs afterwards, in any file.

That is not hypothetical. ``test_http_handler.py`` installs a MagicMock to
prove the initializer reuses an existing instance; the Lambda adapter's
cleanup path then does ``await http_handler._plugin_manager.shutdown()``,
and a plain MagicMock is not awaitable, so ``test_aws_lambda.py`` failed
with "'MagicMock' object can't be awaited". It only stayed hidden because
test_aws_lambda sorts before test_http_handler alphabetically -- running
the two files in the other order, shuffling test order, or renaming either
file surfaces it:

    pytest tests/test_http_handler.py tests/test_aws_lambda.py
    -> 3 failed, 44 passed

Restoring the globals around every test fixes the whole class of problem
rather than the one instance of it.
"""

import pytest


@pytest.fixture(autouse=True)
def reset_http_handler_globals():
    """Snapshot and restore ``server.http_handler``'s module globals.

    Autouse, so a test that stubs these out cannot leak into any later
    test regardless of file or execution order.
    """
    from server import http_handler

    saved_plugin_manager = http_handler._plugin_manager
    saved_mcp_server = http_handler._mcp_server
    saved_config = http_handler._config
    try:
        yield
    finally:
        http_handler._plugin_manager = saved_plugin_manager
        http_handler._mcp_server = saved_mcp_server
        http_handler._config = saved_config
