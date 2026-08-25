"""Guards the autouse fixture that isolates server.http_handler globals.

These two tests are a pair and depend on running in definition order,
which is pytest's default within a file. The first deliberately leaks a
non-awaitable mock into the module globals; the second asserts it did not
survive. If tests/conftest.py's reset fixture is removed or broken, the
second test fails -- which is the point.

Without that fixture the leak reaches server/adapters/aws_lambda.py's
cleanup path, which does ``await _plugin_manager.shutdown()`` and dies with
"'MagicMock' object can't be awaited".
"""

from unittest.mock import MagicMock


def test_a_leaks_mocks_into_handler_globals():
    """Stand-in for any test that stubs the module globals."""
    import server.http_handler

    server.http_handler._plugin_manager = MagicMock()
    server.http_handler._mcp_server = MagicMock()
    server.http_handler._config = MagicMock()


def test_b_globals_were_restored_after_previous_test():
    """The leak from the previous test must not be visible here."""
    import server.http_handler

    assert not isinstance(server.http_handler._plugin_manager, MagicMock), (
        "server.http_handler._plugin_manager leaked from a previous test; "
        "the reset fixture in tests/conftest.py is missing or broken"
    )
    assert not isinstance(server.http_handler._mcp_server, MagicMock), (
        "server.http_handler._mcp_server leaked from a previous test; "
        "the reset fixture in tests/conftest.py is missing or broken"
    )
    assert not isinstance(server.http_handler._config, MagicMock), (
        "server.http_handler._config leaked from a previous test; "
        "the reset fixture in tests/conftest.py is missing or broken"
    )
