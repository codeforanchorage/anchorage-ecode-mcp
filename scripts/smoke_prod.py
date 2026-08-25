"""Smoke test for the Anchorage Municipal Code MCP server.

Exercises the JSON-RPC surface, the transport rules, the caller-error codes
and the structured-output contract end-to-end against a running deployment.
Read-only; paces calls to stay under the API Gateway rate limit (5 rps) and
the WAF per-IP cap (300/5min).

    python scripts/smoke_prod.py                      # prod (custom domain)
    python scripts/smoke_prod.py http://localhost:8000/mcp

WHAT THIS DOES NOT ASSERT, on purpose
-------------------------------------
eCode360 republishes the Anchorage code -- it was last updated 2026-08-12
while this was being written -- so anything pinned to the CONTENT of the
code is a test that will fail one day for being right. There are no
assertions here that a query returns N results, that a given section is the
third hit, or that a chapter has a particular number of children.

What is asserted instead is the CAPABILITY: that a search returns rows at
all, that every guid a search hands back is one get_section accepts, that a
section reports back the same guid it was asked for, and that
structuredContent is present on both a hit and a miss. Those hold whatever
the code says this week.
"""

import json
import sys
import time
import urllib.error
import urllib.request

DEFAULT_URL = "https://anchorage-ecode.codeforanchorage.org/mcp"
URL = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL

PASS = "PASS"
FAIL = "FAIL"
results = []
_id = [0]

try:
    from jsonschema import ValidationError, validate

    HAVE_JSONSCHEMA = True
except ImportError:  # pragma: no cover - smoke script runs outside the venv too
    HAVE_JSONSCHEMA = False


def check(label, ok, detail=""):
    results.append((label, ok))
    mark = PASS if ok else FAIL
    print(f"[{mark}] {label}" + (f" -- {detail}" if detail else ""))


def raw(body, headers=None, method="POST"):
    """Return (status, parsed_body_or_None). Does not raise on 4xx/5xx."""
    req = urllib.request.Request(
        URL,
        data=body.encode() if body is not None else None,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            **(headers or {}),
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            payload = r.read().decode()
            status = r.status
    except urllib.error.HTTPError as e:
        payload = e.read().decode()
        status = e.code
    time.sleep(0.4)  # pace under 5 rps
    try:
        return status, json.loads(payload) if payload else None
    except json.JSONDecodeError:
        return status, None


def rpc(method, params=None, headers=None):
    _id[0] += 1
    payload = {"jsonrpc": "2.0", "id": _id[0], "method": method}
    if params is not None:
        payload["params"] = params
    _status, body = raw(json.dumps(payload), headers=headers)
    return body or {}


def call_tool(name, args):
    return rpc("tools/call", {"name": f"ecode__{name}", "arguments": args})


def text_of(resp):
    return resp["result"]["content"][0]["text"]


def structured_of(resp):
    return resp.get("result", {}).get("structuredContent")


# ── 1. transport ───────────────────────────────────────────────────────────

try:
    r = rpc("ping")
    # The spec defines the ping result as an empty object -- the liveness
    # signal is the successful response itself, not its body.
    check("ping returns {}", r.get("result") == {} and "error" not in r,
          str(r.get("result")))
except Exception as e:
    check("ping returns {}", False, repr(e))

try:
    r = rpc(
        "initialize",
        {"protocolVersion": "2025-11-25", "capabilities": {},
         "clientInfo": {"name": "smoke", "version": "1.0"}},
    )
    si = r["result"]["serverInfo"]
    pv = r["result"]["protocolVersion"]
    # The current revision must be echoed back, not negotiated down.
    check(
        "initialize negotiates 2025-11-25",
        si["name"] == "Anchorage Municipal Code MCP" and pv == "2025-11-25",
        json.dumps({"serverInfo": si, "protocolVersion": pv}),
    )
    check("initialize carries instructions", bool(r["result"].get("instructions")))
except Exception as e:
    check("initialize negotiates 2025-11-25", False, repr(e))

PING = '{"jsonrpc":"2.0","id":99,"method":"ping"}'

for label, headers, expect in [
    ("no Origin is allowed (native clients)", {}, 200),
    ("allowlisted Origin is allowed", {"Origin": "https://claude.ai"}, 200),
    ("disallowed Origin is refused", {"Origin": "https://evil.example"}, 403),
    ("unknown protocol version is refused",
     {"MCP-Protocol-Version": "2026-07-28"}, 400),
    ("supported protocol version passes",
     {"MCP-Protocol-Version": "2025-11-25"}, 200),
]:
    try:
        status, _body = raw(PING, headers=headers)
        check(label, status == expect, f"HTTP {status} (expected {expect})")
    except Exception as e:
        check(label, False, repr(e))

try:
    status, _body = raw(None, method="GET")
    check("GET /mcp is not allowed", status == 405, f"HTTP {status}")
except Exception as e:
    check("GET /mcp is not allowed", False, repr(e))


# ── 2. caller errors are caller errors, not server faults ──────────────────

try:
    r = rpc("server/discover")
    err = r.get("error", {})
    check("unknown method -> -32601", err.get("code") == -32601, str(err.get("code")))
except Exception as e:
    check("unknown method -> -32601", False, repr(e))

try:
    r = rpc("tools/call", {"name": "ecode__no_such_tool", "arguments": {}})
    err = r.get("error", {})
    # The available-tool list rides in `data` so a model can self-correct.
    check(
        "unknown tool -> -32602 with the available list",
        err.get("code") == -32602 and "search_code" in str(err.get("data")),
        str(err.get("code")),
    )
except Exception as e:
    check("unknown tool -> -32602 with the available list", False, repr(e))

try:
    r = rpc("tools/call", {"arguments": {}})
    check("tools/call with no name -> -32602",
          r.get("error", {}).get("code") == -32602, str(r.get("error")))
except Exception as e:
    check("tools/call with no name -> -32602", False, repr(e))

try:
    r = rpc("tools/call", {"name": "ecode__search_code", "arguments": "query=dogs"})
    err = r.get("error", {})
    # Before the fix this reached the plugin and came back as a tool RESULT
    # reading "'str' object has no attribute 'get'".
    check(
        "non-object arguments -> -32602, not a tool result",
        err.get("code") == -32602 and "result" not in r,
        str(err.get("code")),
    )
except Exception as e:
    check("non-object arguments -> -32602, not a tool result", False, repr(e))

try:
    r = call_tool("search_code", {"query": "dog", "page": "two"})
    msg = text_of(r)
    check(
        "bad page names the argument and the value",
        "page" in msg and "two" in msg and "invalid literal" not in msg,
        msg[:70],
    )
except Exception as e:
    check("bad page names the argument and the value", False, repr(e))

try:
    r = call_tool("get_section", {"guid": "21.05.010"})
    msg = text_of(r)
    check(
        "section number as guid is named as such",
        "section NUMBER" in msg and "search_code" in msg,
        msg[:70],
    )
except Exception as e:
    check("section number as guid is named as such", False, repr(e))


# ── 3. tool metadata ───────────────────────────────────────────────────────

schemas = {}
try:
    r = rpc("tools/list")
    tools = {t["name"]: t for t in r["result"]["tools"]}
    schemas = {n: t.get("outputSchema") for n, t in tools.items()}

    check("tools/list returns the eCode tool set",
          set(tools) == {
              "ecode__search_code",
              "ecode__get_section",
              "ecode__browse_structure",
              "ecode__get_table_of_contents",
              "ecode__get_customer_info",
          },
          f"{len(tools)} tools")

    check("every tool has a display title",
          all(t.get("title") for t in tools.values()),
          ", ".join(sorted(t.get("title", "?") for t in tools.values())))

    check(
        "every tool is read-only without idempotentHint",
        all(
            t.get("annotations", {}).get("readOnlyHint") is True
            and "idempotentHint" not in t.get("annotations", {})
            for t in tools.values()
        ),
    )

    with_schema = {n for n, s in schemas.items() if s}
    check(
        "the four data tools declare an outputSchema",
        with_schema == {
            "ecode__search_code",
            "ecode__get_section",
            "ecode__browse_structure",
            "ecode__get_table_of_contents",
        },
        ", ".join(sorted(with_schema)),
    )
except Exception as e:
    check("tools/list returns the eCode tool set", False, repr(e))


def conforms(tool_name, structured):
    """Validate against the schema the server itself advertised."""
    if not HAVE_JSONSCHEMA or not schemas.get(tool_name):
        return True, "jsonschema unavailable; shape not validated"
    try:
        validate(structured, schemas[tool_name])
        return True, ""
    except ValidationError as e:
        return False, e.message[:80]


# ── 4. structured output: a hit, a miss, and a page past the end ───────────

first_guid = None
try:
    r = call_tool("search_code", {"query": "dog license", "page": 0})
    sc = structured_of(r)
    ok = sc is not None and len(sc.get("rows", [])) > 0
    if ok:
        # Capability, not scope: every row must be chainable, whatever the
        # code happens to say this week.
        ok = all(row.get("guid") and row.get("url") for row in sc["rows"])
        first_guid = sc["rows"][0]["guid"]
    valid, why = conforms("ecode__search_code", sc) if sc else (False, "no structured")
    check("search hit: structuredContent present and conforming",
          bool(ok and valid), why or f"{len(sc.get('rows', [])) if sc else 0} rows")
except Exception as e:
    check("search hit: structuredContent present and conforming", False, repr(e))

try:
    r = call_tool("search_code", {"query": "zzzqqxnotarealword", "page": 0})
    sc = structured_of(r)
    valid, why = conforms("ecode__search_code", sc) if sc else (False, "no structured")
    # A search that ran and matched nothing is a KNOWN, COMPLETE count of 0 --
    # not null, which would mean unmeasured.
    check(
        "search miss: structuredContent present, rows [], total_count 0",
        sc is not None and sc.get("rows") == [] and sc.get("total_count") == 0
        and valid,
        why or f"total_count={sc.get('total_count') if sc else None!r}",
    )
except Exception as e:
    check("search miss: structuredContent present, rows [], total_count 0",
          False, repr(e))

try:
    r = call_tool("search_code", {"query": "dog license", "page": 999})
    sc = structured_of(r)
    # A page past the end must not report a complete count of zero for a
    # query that has matches -- that says "no such law exists" about law
    # that does.
    check(
        "page past the end reports the real total, not a false zero",
        sc is not None and sc.get("rows") == [] and (sc.get("total_count") or 0) > 0,
        f"total_count={sc.get('total_count') if sc else None!r}",
    )
except Exception as e:
    check("page past the end reports the real total, not a false zero",
          False, repr(e))


# ── 5. the round trip that makes search useful ─────────────────────────────

if first_guid:
    try:
        r = call_tool("get_section", {"guid": first_guid})
        sc = structured_of(r)
        valid, why = conforms("ecode__get_section", sc) if sc else (
            False, "no structured")
        # The section must report back the guid it was asked for. A citation
        # that names a different section than the one read is this server's
        # worst failure mode, and it would look completely normal.
        check(
            "a search guid resolves and reports its own guid back",
            bool(sc and sc.get("guid") == first_guid and sc.get("url") and valid),
            why or f"guid={sc.get('guid') if sc else None!r}",
        )
        check(
            "the section carries a citable ref and full text",
            bool(sc and sc.get("text") and sc.get("url", "").startswith("http")),
            f"ref={sc.get('ref') if sc else None!r}",
        )
    except Exception as e:
        check("a search guid resolves and reports its own guid back", False, repr(e))


# ── 6. outline tools ───────────────────────────────────────────────────────

try:
    r = call_tool("browse_structure", {"guid": "ROOT"})
    sc = structured_of(r)
    valid, why = conforms("ecode__browse_structure", sc) if sc else (
        False, "no structured")
    ok = bool(sc and sc.get("children") and valid)
    if ok:
        # Every child must be walkable, and must say what KIND of node it is.
        ok = all(c.get("guid") for c in sc["children"])
        ok = ok and any(c.get("label") for c in sc["children"])
    check("browse_structure(ROOT): walkable children with a node kind", ok,
          why or f"{len(sc.get('children', [])) if sc else 0} children")
except Exception as e:
    check("browse_structure(ROOT): walkable children with a node kind",
          False, repr(e))

try:
    r = call_tool("get_table_of_contents", {})
    sc = structured_of(r)
    valid, why = conforms("ecode__get_table_of_contents", sc) if sc else (
        False, "no structured")
    check("table of contents: structuredContent present and conforming",
          bool(sc and sc.get("children") and valid),
          why or f"{len(sc.get('children', [])) if sc else 0} entries")
except Exception as e:
    check("table of contents: structuredContent present and conforming",
          False, repr(e))


# ── 7. the tool that deliberately has no schema ────────────────────────────

try:
    r = call_tool("get_customer_info", {})
    txt = text_of(r)
    # Deliberately declares no outputSchema: it is metadata prose whose value
    # IS the prose. So it must NOT emit structuredContent either.
    check(
        "customer info returns prose and no structuredContent",
        "AN6998" in txt and structured_of(r) is None,
        txt.splitlines()[0][:60] if txt else "",
    )
except Exception as e:
    check("customer info returns prose and no structuredContent", False, repr(e))


print("\n=== SUMMARY ===")
print(f"target: {URL}")
if not HAVE_JSONSCHEMA:
    print("note: jsonschema not installed -- structured shapes were NOT validated")
n_pass = sum(1 for _, ok in results if ok)
print(f"{n_pass}/{len(results)} checks passed")
for label, ok in results:
    if not ok:
        print(f"  FAILED: {label}")
sys.exit(0 if n_pass == len(results) else 1)
