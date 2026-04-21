"""Stdio-to-HTTP bridge for connecting Claude to OpenContext MCP server.

Reads JSON-RPC messages from stdin, forwards them to the local HTTP server,
and writes responses to stdout. This bridges Claude's stdio MCP transport
to the OpenContext HTTP-based MCP server.
"""

import json
import sys
import urllib.request
import urllib.error

SERVER_URL = "http://localhost:8000/mcp"


def main():
    url = sys.argv[1] if len(sys.argv) > 1 else SERVER_URL
    if not url.endswith("/mcp"):
        url = url.rstrip("/") + "/mcp"

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            error_resp = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": "Parse error"},
            }
            print(json.dumps(error_resp), flush=True)
            continue

        is_notification = request.get("id") is None

        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(request).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=120) as resp:
                body = resp.read().decode("utf-8")

            if not body:
                if is_notification:
                    continue
                error_resp = {
                    "jsonrpc": "2.0",
                    "id": request.get("id"),
                    "error": {"code": -32603, "message": "Empty response"},
                }
                print(json.dumps(error_resp), flush=True)
                continue

            response = json.loads(body)
            if is_notification:
                continue
            print(json.dumps(response), flush=True)

        except urllib.error.HTTPError as e:
            if is_notification:
                continue
            error_resp = {
                "jsonrpc": "2.0",
                "id": request.get("id"),
                "error": {"code": -32603, "message": f"HTTP {e.code}"},
            }
            print(json.dumps(error_resp), flush=True)

        except Exception as e:
            if is_notification:
                continue
            error_resp = {
                "jsonrpc": "2.0",
                "id": request.get("id"),
                "error": {"code": -32603, "message": str(e)},
            }
            print(json.dumps(error_resp), flush=True)


if __name__ == "__main__":
    main()
