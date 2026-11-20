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
def clear_ecode_static_cache():
    """Empty the plugin's module-level TOC / customer cache around each test.

    The cache is module-level so it survives the per-invocation plugin
    rebuild on Lambda; in tests that same property would let one test's
    table of contents answer another's.
    """
    from plugins.ecode import plugin as ecode_plugin

    ecode_plugin._STATIC_CACHE.clear()
    ecode_plugin._RECENT_CACHE.clear()
    try:
        yield
    finally:
        ecode_plugin._STATIC_CACHE.clear()
        ecode_plugin._RECENT_CACHE.clear()


@pytest.fixture(autouse=True)
def no_code_date_lookup(request, monkeypatch):
    """Stop get_section / get_sections from looking up the code's
    last-updated date, unless the test is marked `code_date`.

    The lookup is one more upstream call, which would otherwise have to be
    supplied by every test that scripts the client's responses in order.
    """
    if request.node.get_closest_marker("code_date"):
        return
    from plugins.ecode.plugin import EcodePlugin

    async def unknown(self):
        return None

    monkeypatch.setattr(EcodePlugin, "_code_last_updated", unknown)


@pytest.fixture(autouse=True)
def section_numbers_by_search(request, monkeypatch):
    """Resolve section numbers by search, as before 1.17.0, unless the
    test is marked `outline_sections`.

    Since 1.17.0 a number is looked up in the table of contents first,
    which is one more upstream call ahead of the ones most tests script
    in order. The search path is still the fallback in production, so
    those tests keep covering real behaviour.
    """
    if request.node.get_closest_marker("outline_sections"):
        return
    from plugins.ecode import plugin as ecode_plugin

    monkeypatch.setattr(ecode_plugin, "RESOLVE_SECTIONS_FROM_OUTLINE", False)


@pytest.fixture(autouse=True)
def no_ordinance_title_download(request, monkeypatch):
    """Stop find_ordinance / recent_ordinances from downloading ordinance
    PDFs to read their titles, unless the test is marked `ordinance_titles`.

    The download goes to the publisher's public site through a client the
    usual mocks do not cover, so an unmarked test would reach the network.
    """
    from plugins.ecode import plugin as ecode_plugin

    ecode_plugin._ORDINANCE_TITLE_CACHE.clear()
    if request.node.get_closest_marker("ordinance_titles"):
        return

    async def unread(self, url):
        return None

    monkeypatch.setattr(ecode_plugin.EcodePlugin, "_ordinance_title", unread)


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
