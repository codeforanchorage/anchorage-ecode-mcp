"""The local dev server must behave like prod, not merely resemble it.

scripts/local_server.py used to call MCPServer.handle_http_request directly,
skipping UniversalHTTPHandler entirely -- so the Origin allowlist, the
MCP-Protocol-Version check, and path/method validation were all absent
locally. Anything built in those areas was unreproducible and unregressable
outside production.

These tests assert the adapter is a thin shim over the same handler the
Lambda adapter uses, so a future edit cannot quietly re-introduce a second
implementation of the request path.
"""

import ast
import pathlib

import pytest

LOCAL_SERVER = (
    pathlib.Path(__file__).resolve().parent.parent / "scripts" / "local_server.py"
)
SOURCE = LOCAL_SERVER.read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)


def _attribute_calls() -> set:
    """Every ``x.y(...)`` attribute name actually CALLED in the module.

    Deliberately AST-based: a substring search over the source also matches
    the comments explaining why these calls must not come back, so it can
    never go green.
    """
    return {
        node.func.attr
        for node in ast.walk(TREE)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }


class TestRoutesThroughTheSharedHandler:
    def test_uses_universal_http_handler(self):
        called = {
            node.func.id
            for node in ast.walk(TREE)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert "UniversalHTTPHandler" in called

    def test_does_not_call_mcp_server_directly(self):
        """The bypass that made local dev unable to reproduce prod."""
        assert "handle_http_request" not in _attribute_calls(), (
            "scripts/local_server.py calls MCPServer.handle_http_request "
            "directly again; that skips the Origin allowlist, the "
            "MCP-Protocol-Version check and path/method validation"
        )

    def test_does_not_build_its_own_plugin_manager(self):
        """A second PluginManager/MCPServer pair is a third copy of the
        process-wide state that already leaks between test files."""
        called = {
            node.func.id
            for node in ast.walk(TREE)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert "PluginManager" not in called
        assert "MCPServer" not in called

    def test_wires_options_to_handle_options(self):
        """Without this the preflight never reaches the 403 Origin check."""
        calls = _attribute_calls()
        assert "add_options" in calls
        assert "handle_options" in calls


class TestBodyDecodingMatchesProd:
    """API Gateway hands the Lambda an already-decoded string with invalid
    bytes replaced, so prod answers a malformed body with a normal JSON-RPC
    error. aiohttp's request.text() raises instead, which escaped as a 500.

    Reproduced with: curl -d on Git Bash re-encodes a non-ASCII argument to
    the ANSI codepage, so U+00A7 arrives as a lone 0xA7 byte.
    """

    def test_does_not_use_request_text(self):
        assert "text" not in _attribute_calls(), (
            "request.text() raises UnicodeDecodeError on a malformed body "
            "and escapes as an aiohttp 500; prod replaces invalid bytes and "
            "returns a JSON-RPC error -- read() and decode with "
            'errors="replace" instead'
        )

    def test_decodes_leniently(self):
        replace_kwargs = [
            kw
            for node in ast.walk(TREE)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "decode"
            for kw in node.keywords
            if kw.arg == "errors"
            and isinstance(kw.value, ast.Constant)
            and kw.value.value == "replace"
        ]
        assert replace_kwargs, 'expected a .decode(..., errors="replace")'

    def test_lone_continuation_byte_decodes_rather_than_raising(self):
        """The exact byte sequence that produced the 500."""
        raw = b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":' \
              b'{"name":"ecode__get_section","arguments":{"guid":"\xa7 21.05.010"}}}'

        with pytest.raises(UnicodeDecodeError):
            raw.decode("utf-8")

        decoded = raw.decode("utf-8", errors="replace")
        assert "21.05.010" in decoded
        assert "�" in decoded


class TestSingleEntryPoint:
    def test_root_local_server_is_gone(self):
        """Two entry points is how they drifted apart to begin with."""
        root_copy = LOCAL_SERVER.parent.parent / "local_server.py"
        assert not root_copy.exists(), (
            "a second local server reappeared at the repo root; keep "
            "scripts/local_server.py as the only one"
        )

    def test_second_lambda_handler_is_gone(self):
        """server/lambda_handler.py carried its own _plugin_manager /
        _mcp_server globals -- a third copy of process-wide state."""
        dead = LOCAL_SERVER.parent.parent / "server" / "lambda_handler.py"
        assert not dead.exists(), (
            "server/lambda_handler.py is back; Terraform points at "
            "server.adapters.aws_lambda.lambda_handler, which is the only "
            "entry point that should exist"
        )

    def test_startup_prints_are_ascii(self):
        """Emoji in the startup banner crashed the server on a Windows
        console unless PYTHONIOENCODING=utf-8 was set."""
        tree = ast.parse(SOURCE)
        offenders = []
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "print"
            ):
                for arg in ast.walk(node):
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        if not arg.value.isascii():
                            offenders.append(f"line {node.lineno}: {arg.value!r}")
        assert not offenders, (
            "non-ASCII in a startup print; this crashes on a cp1252 console "
            "unless PYTHONIOENCODING=utf-8 is set:\n  " + "\n  ".join(offenders)
        )
