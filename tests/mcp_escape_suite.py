#!/usr/bin/env python3
"""Containment test suite for the /mcp connector (docs/plan-mcp-connector.md).

Same fixture, same pass criteria, and largely the same attack list as
tests/escape_suite.py -- driven through JSON-RPC `tools/call` instead of the
REST request line. The attack surface differs in one important way: paths
arrive as JSON string VALUES, not as a raw HTTP request-line, so URL
normalisation and percent-/double-encoding are not in play (there is no
decode step between the JSON parser and app.safepath). What IS in play is
JSON string escapes: `\\u0000`, lone UTF-16 surrogates, and a literal `..` in
every position an argument accepts one.

Run against an already-running container (see tests/run_tests.sh):

    BASE_URL=127.0.0.1:18080 API_TOKEN=... FIXTURE_REPO=... MODE=isolated \\
      python3 tests/mcp_escape_suite.py

`stateless_http=True` (app/mcp_endpoint.py) means every POST to /mcp is a
self-contained JSON-RPC call -- no `initialize` handshake or session id is
needed before `tools/list`/`tools/call` (docs/plan-mcp-connector.md,
"Transport mode: stateless and JSON, not SSE", verified against the real
SDK). `json_response=True` means the response body is one JSON object, not
an SSE stream, so a plain `http.client` request/response round-trip is
enough -- no SSE client needed.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

BASE = os.environ.get("BASE_URL", "127.0.0.1:18080")
TOKEN = os.environ["API_TOKEN"]
RO_TOKEN = os.environ.get("API_TOKEN_READONLY", "")
REPO = Path(os.environ["FIXTURE_REPO"])
MODE = os.environ.get("MODE", "unknown")
# Must match what tests/run_tests.sh passes as container env, so this suite
# knows exactly where the truncation/refusal boundaries are.
INLINE_LIMIT_CHARS = int(os.environ.get("MCP_INLINE_LIMIT_CHARS", "500"))
BASE64_LIMIT_BYTES = int(os.environ.get("MCP_BASE64_LIMIT_BYTES", "1024"))

CANARIES = [
    b"CANARY_SECRETS_ENV_a1b2c3d4",
    b"CANARY_GIT_CONFIG_e5f6a7b8",
    b"CANARY_DEPLOY_KEY_c9d0e1f2",
    b"root:x:0:0",          # /etc/passwd
    b"ordinary source file",
]

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"


@dataclass
class Results:
    passed: int = 0
    failed: int = 0
    failures: list[str] = field(default_factory=list)

    def ok(self, kind: str, name: str, note: str = "") -> None:
        self.passed += 1
        print(f"  {GREEN}PASS{RESET} [{kind}] {name} {DIM}{note}{RESET}")

    def bad(self, kind: str, name: str, why: str) -> None:
        self.failed += 1
        self.failures.append(f"[{kind}] {name}: {why}")
        print(f"  {RED}FAIL{RESET} [{kind}] {name} -- {why}")


R = Results()


# ==========================================================================
# JSON-RPC transport
# ==========================================================================

def mcp_raw(body: bytes, *, path: str = "/mcp", token: str | None = TOKEN,
            api_key: str | None = None, headers: dict | None = None,
            ) -> tuple[int, dict, bytes]:
    """POST a raw body to /mcp and return (status, headers, body) -- no
    redirect following, so a 307/308 shows up as itself rather than being
    silently chased away (test 6, "bare /mcp does not redirect")."""
    host, _, port = BASE.partition(":")
    conn = http.client.HTTPConnection(host, int(port or 80), timeout=30)
    hdrs = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    hdrs.update(headers or {})
    if token:
        hdrs["Authorization"] = f"Bearer {token}"
    if api_key:
        hdrs["X-API-Key"] = api_key
    try:
        conn.request("POST", path, body=body, headers=hdrs)
        resp = conn.getresponse()
        data = resp.read()
        return resp.status, dict(resp.getheaders()), data
    finally:
        conn.close()


def mcp_call(method: str, params: dict | None = None, *, id_: int = 1,
             **kw) -> tuple[int, dict, bytes]:
    body = json.dumps({
        "jsonrpc": "2.0", "id": id_, "method": method, "params": params or {},
    }).encode()
    return mcp_raw(body, **kw)


def call_tool(name: str, arguments: dict, **kw) -> tuple[int, bool, Any]:
    """Call a tool; return (http_status, is_error, payload).

    `payload` is the tool's returned dict on success, or the raw error text
    on failure (FastMCP wraps a raised exception as a JSON-RPC result with
    isError: true and HTTP 200 -- it never becomes a non-200 status, which
    is exactly why app/mcp_endpoint.py's `_guard` has to log denials itself;
    see tests/run_tests.sh's "denied MCP" check).
    """
    status, _hdrs, raw = mcp_call("tools/call", {"name": name, "arguments": arguments}, **kw)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return status, True, raw.decode("utf-8", "replace")
    if "error" in parsed:
        # A transport/protocol-level JSON-RPC error (e.g. bad auth already
        # handled at the middleware level, or a malformed request) rather
        # than a tool-level error result.
        return status, True, parsed["error"]
    result = parsed.get("result", {})
    is_error = bool(result.get("isError"))
    content = result.get("content") or []
    text = content[0].get("text", "") if content else ""
    if is_error:
        return status, True, text
    try:
        return status, False, json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return status, False, text


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def snapshot() -> dict[str, str]:
    out = {}
    for p in sorted(REPO.rglob("*")):
        if "shared" in p.relative_to(REPO).parts:
            continue
        if p.is_file() and not p.is_symlink():
            out[str(p.relative_to(REPO))] = sha(p)
        elif p.is_dir():
            out[str(p.relative_to(REPO)) + "/"] = "dir"
    return out


def leaked(payload: Any) -> str | None:
    blob = payload if isinstance(payload, (bytes, bytearray)) else str(payload).encode()
    for c in CANARIES:
        if c in blob:
            return c.decode()
    return None


# ==========================================================================
# FUNCTIONAL -- the connector works inside the shared folder
# ==========================================================================

def functional() -> None:
    print(f"\n{YELLOW}FUNCTIONAL -- the MCP connector works inside the shared folder{RESET}")

    status, _h, raw = mcp_call("tools/list")
    tools = {t["name"] for t in json.loads(raw).get("result", {}).get("tools", [])} \
        if status == 200 else set()
    expected = {
        "get_folder_info", "list_files", "stat_file", "read_file",
        "write_file", "delete_file", "make_directory", "move_file",
    }
    if expected <= tools:
        R.ok("func", "all eight tools are registered", f"({sorted(tools)})")
    else:
        R.bad("func", "tool registration", f"got {sorted(tools)}, missing {expected - tools}")

    status, is_err, info = call_tool("get_folder_info", {})
    if status == 200 and not is_err and info.get("token_scope") == "rw":
        R.ok("func", "get_folder_info reports rw scope for the full token")
    else:
        R.bad("func", "get_folder_info", f"status {status} is_err {is_err} -> {info!r}"[:200])

    status, is_err, out = call_tool("write_file", {"path": "mcp-report.txt", "content": "agent wrote this via mcp\n"})
    if status == 200 and not is_err and out.get("ok"):
        R.ok("func", "write_file creates a file")
    else:
        R.bad("func", "write_file create", f"status {status} -> {out!r}"[:200])

    status, is_err, out = call_tool("read_file", {"path": "mcp-report.txt"})
    if status == 200 and not is_err and out.get("text") == "agent wrote this via mcp\n":
        R.ok("func", "read_file round-trips the exact text")
    else:
        R.bad("func", "read_file round-trip", f"status {status} -> {out!r}"[:200])

    status, is_err, out = call_tool("write_file", {"path": "mcp-report.txt", "content": "replaced", "overwrite": True})
    status2, _e2, out2 = call_tool("read_file", {"path": "mcp-report.txt"})
    if status == 200 and out2.get("text") == "replaced":
        R.ok("func", "write_file(overwrite=true) replaces an existing file")
    else:
        R.bad("func", "overwrite=true", f"got {status}/{status2}, text {out2!r}"[:200])

    status, is_err, out = call_tool("write_file", {"path": "mcp-once.txt", "content": "first", "overwrite": False})
    status2, is_err2, out2 = call_tool("write_file", {"path": "mcp-once.txt", "content": "second", "overwrite": False})
    if not is_err and is_err2:
        R.ok("func", "write_file(overwrite=false) refuses an existing file")
    else:
        R.bad("func", "overwrite=false", f"is_err {is_err}/{is_err2}")

    status, is_err, out = call_tool("write_file", {"path": "mcp-deep/a/b.txt", "content": "deep"})
    status2, _e2, out2 = call_tool("read_file", {"path": "mcp-deep/a/b.txt"})
    if not is_err and out2.get("text") == "deep":
        R.ok("func", "write_file defaults parents=true (no explicit mkdir needed)")
    else:
        R.bad("func", "write_file parents default", f"is_err {is_err}, read {out2!r}"[:200])

    status, is_err, out = call_tool("make_directory", {"path": "mcp-dir/nested"})
    status2, is_err2, out2 = call_tool("make_directory", {"path": "mcp-dir/nested"})
    if not is_err and out.get("created") is True and not is_err2 and out2.get("created") is False:
        R.ok("func", "make_directory is idempotent (created: false on repeat)")
    else:
        R.bad("func", "make_directory idempotent", f"{out!r} then {out2!r}"[:200])

    status, is_err, out = call_tool("list_files", {"path": "", "recursive": True})
    paths = {e["path"] for e in out.get("entries", [])} if not is_err else set()
    if "mcp-deep/a/b.txt" in paths:
        R.ok("func", "list_files(recursive=true) descends")
    else:
        R.bad("func", "list_files recursive", f"paths {sorted(paths)[:10]}")

    status, is_err, out = call_tool("stat_file", {"path": "mcp-report.txt"})
    if not is_err and out.get("type") == "file":
        R.ok("func", "stat_file reports type")
    else:
        R.bad("func", "stat_file", f"{out!r}"[:200])

    status, is_err, out = call_tool("move_file", {"src": "mcp-once.txt", "dst": "mcp-dir/moved.txt"})
    status2, _e2, out2 = call_tool("read_file", {"path": "mcp-dir/moved.txt"})
    if not is_err and out2.get("text") == "first":
        R.ok("func", "move_file moves within the folder")
    else:
        R.bad("func", "move_file", f"{out!r} / {out2!r}"[:200])

    status, is_err, out = call_tool("delete_file", {"path": "mcp-report.txt"})
    status2, is_err2, out2 = call_tool("read_file", {"path": "mcp-report.txt"})
    if not is_err and is_err2:
        R.ok("func", "delete_file removes a file")
    else:
        R.bad("func", "delete_file", f"is_err {is_err}/{is_err2}")

    # --- read-only token: every write tool refused, same message text -----
    if RO_TOKEN:
        checks = [
            ("write_file", {"path": "ro-should-fail.txt", "content": "x"}),
            ("delete_file", {"path": "mcp-dir/moved.txt"}),
            ("make_directory", {"path": "ro-should-fail-dir"}),
            ("move_file", {"src": "mcp-dir/moved.txt", "dst": "mcp-dir/moved2.txt"}),
        ]
        all_refused = True
        for tool, args in checks:
            status, is_err, payload = call_tool(tool, args, token=RO_TOKEN)
            refused = is_err and "read-only" in str(payload)
            all_refused = all_refused and refused
            if not refused:
                R.bad("func", f"read-only token refuses {tool}", f"is_err {is_err} -> {payload!r}"[:200])
        if all_refused:
            R.ok("func", "read-only token refuses every write tool, same message as REST")

        status, is_err, out = call_tool("list_files", {}, token=RO_TOKEN)
        if not is_err:
            R.ok("func", "read-only token can still read")
        else:
            R.bad("func", "read-only token read", f"{out!r}"[:200])

    # --- auth gate: unauthenticated tools/call never reaches a tool -------
    status, _h, raw = mcp_call("tools/call", {"name": "list_files", "arguments": {}}, token=None)
    if status == 401:
        R.ok("func", "unauthenticated tools/call -> 401, not routed to a tool")
    else:
        R.bad("func", "unauthenticated tools/call", f"status {status}")

    status, _h, raw = mcp_call("tools/call", {"name": "list_files", "arguments": {}},
                                token=None, api_key=TOKEN)
    if status == 200:
        R.ok("func", "X-API-Key clears the /mcp auth gate")
    else:
        R.bad("func", "X-API-Key on /mcp", f"status {status}")

    status, _h, raw = mcp_call("tools/call", {"name": "list_files", "arguments": {}},
                                token="wrong-token-0123456789abcdef")
    if status == 401:
        R.ok("func", "wrong token on /mcp -> 401")
    else:
        R.bad("func", "wrong token on /mcp", f"status {status}")

    # --- bare /mcp (no trailing slash) must not redirect -------------------
    status, headers, _raw = mcp_raw(
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}).encode(),
        path="/mcp",
    )
    if status not in (307, 308):
        R.ok("func", "POST /mcp (no trailing slash) does not redirect", f"-> {status}")
    else:
        R.bad("func", "bare /mcp redirect", f"status {status} location {headers.get('location')}")

    # --- security headers on an MCP response --------------------------
    status, headers, _raw = mcp_call("tools/list")
    hdr = {k.lower(): v for k, v in headers.items()}
    if (hdr.get("x-content-type-options") == "nosniff"
            and hdr.get("referrer-policy") == "no-referrer"
            and hdr.get("cache-control") == "no-store"):
        R.ok("func", "security headers present on an /mcp response")
    else:
        R.bad("func", "security headers", f"{hdr}"[:200])

    # --- budgets: inline text, truncation, binary refusal, base64 ---------
    small_text = "y" * (INLINE_LIMIT_CHARS - 10)
    call_tool("write_file", {"path": "mcp-budget-small.txt", "content": small_text})
    status, is_err, out = call_tool("read_file", {"path": "mcp-budget-small.txt"})
    if not is_err and out.get("text") == small_text and "truncated" not in out:
        R.ok("func", "read_file under budget returns inline text, no truncation markers")
    else:
        R.bad("func", "read_file under budget", f"is_err {is_err} -> keys {list(out) if isinstance(out, dict) else out}")

    # Comfortably larger than both the configured budget and the server's
    # own PREVIEW_CHARS (2000), so the preview is a genuine, strict prefix.
    big_text = "z" * (INLINE_LIMIT_CHARS + 10_000)
    call_tool("write_file", {"path": "mcp-budget-big.txt", "content": big_text})
    status, is_err, out = call_tool("read_file", {"path": "mcp-budget-big.txt"})
    if (not is_err and out.get("truncated") is True and out.get("bytes") == len(big_text.encode())
            and big_text.startswith(out.get("preview", "\0")) and "note" in out):
        R.ok("func", "read_file over budget: truncated + byte count + note, not silent")
    else:
        R.bad("func", "read_file over budget", f"is_err {is_err} -> {str(out)[:200]!r}")

    binary_path = REPO / "shared" / "mcp-binary.bin"
    binary_path.write_bytes(b"\x00\x01\x02not-utf8\xff\xfe")
    status, is_err, out = call_tool("read_file", {"path": "mcp-binary.bin"})
    if is_err and "GET /v1/content" in str(out):
        R.ok("func", "read_file refuses a NUL-containing file, names the REST endpoint")
    else:
        R.bad("func", "binary refusal", f"is_err {is_err} -> {str(out)[:200]!r}")

    status, is_err, out = call_tool("read_file", {"path": "mcp-binary.bin", "encoding": "base64"})
    if not is_err and out.get("encoding") == "base64":
        import base64 as _b64
        if _b64.b64decode(out["content"]) == binary_path.read_bytes():
            R.ok("func", "read_file(encoding=base64) round-trips binary content")
        else:
            R.bad("func", "base64 round-trip", "decoded bytes do not match")
    else:
        R.bad("func", "base64 read", f"is_err {is_err} -> {str(out)[:200]!r}")

    over_cap = b"\xff" * (BASE64_LIMIT_BYTES + 1)
    (REPO / "shared" / "mcp-binary-big.bin").write_bytes(over_cap)
    status, is_err, out = call_tool("read_file", {"path": "mcp-binary-big.bin", "encoding": "base64"})
    if is_err and "base64 limit" in str(out):
        R.ok("func", "read_file(encoding=base64) over the byte cap is refused")
    else:
        R.bad("func", "base64 cap (read)", f"is_err {is_err} -> {str(out)[:200]!r}")

    import base64 as _b64
    status, is_err, out = call_tool("write_file", {
        "path": "mcp-write-base64-big.bin",
        "content": _b64.b64encode(over_cap).decode("ascii"),
        "encoding": "base64",
    })
    if is_err and "base64 limit" in str(out):
        R.ok("func", "write_file(encoding=base64) over the byte cap is refused")
    else:
        R.bad("func", "base64 cap (write)", f"is_err {is_err} -> {str(out)[:200]!r}")


# ==========================================================================
# ESCAPE -- every hostile path must fail, exactly as it does over REST
# ==========================================================================

# JSON string values, not URL paths: percent-/double-encoding has no
# equivalent here (there is no decode step between the JSON parser and
# app.safepath), but literal ".." in every position, NUL bytes, and lone
# UTF-16 surrogates all transmit fine as JSON string escapes.
HOSTILE_PATHS = [
    ("raw ..",                   "../secrets.env"),
    ("raw ../..",                "../../etc/passwd"),
    ("deep ../ chain",           "../../../../../../etc/passwd"),
    ("traversal mid-path",       "nested/../../secrets.env"),
    ("dot segment",              "./../secrets.env"),
    ("dot-dot-slash padding",    "....//secrets.env"),
    ("absolute path",            "/etc/passwd"),
    ("backslash traversal",      "..\\..\\secrets.env"),
    ("NUL byte truncation",      "existing.txt\x00../../secrets.env"),
    ("lone high surrogate",      "\ud800secrets.env"),
    ("lone low surrogate",       "\udc00secrets.env"),
    ("symlink to outside file",  "link-to-secret"),
    ("symlink dir component",    "escape-dir/secrets.env"),
    ("symlink to /",             "rootlink/etc/passwd"),
    ("nested symlink",           "nested/deep-link"),
    ("hard link to outside file", "hardlink-to-secret"),
    ("reach .git by name",       ".git/config"),
    ("reach .git via symlink dir", "escape-dir/.git/config"),
]


def escape_read(name: str, path: str) -> None:
    status, is_error, payload = call_tool("read_file", {"path": path})
    hit = leaked(payload)
    if hit:
        R.bad("mcp-escape", name, f"LEAKED {hit!r} (status {status})")
    elif not is_error:
        R.bad("mcp-escape", name, f"returned success: {str(payload)[:160]!r}")
    else:
        R.ok("mcp-escape", name, f"-> tool error{'' if status == 200 else f' (http {status})'}")


def escape_list(name: str, path: str) -> None:
    status, is_error, payload = call_tool("list_files", {"path": path})
    hit = leaked(payload)
    if hit:
        R.bad("mcp-escape", name, f"LEAKED {hit!r}")
    elif not is_error:
        R.bad("mcp-escape", name, f"returned success: {str(payload)[:160]!r}")
    else:
        R.ok("mcp-escape", name, "-> tool error")


def escape_write(name: str, path: str, victim: Path) -> None:
    before = sha(victim) if victim.exists() else None
    status, is_error, payload = call_tool("write_file", {"path": path, "content": "PWNED_BY_MCP_TEST"})
    after = sha(victim) if victim.exists() else None
    if before != after:
        R.bad("mcp-escape", name, f"MODIFIED {victim}")
    elif not is_error:
        R.bad("mcp-escape", name, f"write returned success: {payload!r}"[:200])
    else:
        R.ok("mcp-escape", name, "-> tool error")


def escapes() -> None:
    print(f"\n{YELLOW}ESCAPE -- these must all fail, over /mcp tools/call{RESET}")

    secrets = REPO / "secrets.env"

    for name, path in HOSTILE_PATHS:
        escape_read(f"read_file: {name}", path)

    for name, path in [
        ("list escape-dir",  "escape-dir"),
        ("list rootlink",    "rootlink"),
        ("list ..",          ".."),
    ]:
        escape_list(name, path)

    for name, path in [
        ("write through ..",       "../secrets.env"),
        ("write through symlink",  "link-to-secret"),
        ("write via symlinked dir", "escape-dir/secrets.env"),
        ("create through ..",      "../planted-mcp.txt"),
        ("write into .git",        ".git/hooks/evil"),
    ]:
        escape_write(f"write_file: {name}", path, secrets)

    # mkdir must not create a directory outside the shared folder
    status, is_error, _payload = call_tool("make_directory", {"path": "../planted-mcp-dir"})
    if is_error and not (REPO / "planted-mcp-dir").exists():
        R.ok("mcp-escape", "make_directory through ..", "-> tool error")
    else:
        R.bad("mcp-escape", "make_directory through ..",
              f"is_error {is_error}, dir exists={(REPO / 'planted-mcp-dir').exists()}")

    status, is_error, _payload = call_tool("make_directory", {"path": "escape-dir/planted-mcp-dir"})
    if is_error and not (REPO / "planted-mcp-dir").exists():
        R.ok("mcp-escape", "make_directory through symlink", "-> tool error")
    else:
        R.bad("mcp-escape", "make_directory through symlink", f"is_error {is_error}")

    # move must not read from or write to outside the shared folder
    for label, src, dst in [
        ("move src outside", "../secrets.env", "mcp-stolen.txt"),
        ("move dst outside", "existing.txt", "../mcp-planted.txt"),
        ("move via symlink", "escape-dir/secrets.env", "mcp-stolen.txt"),
    ]:
        status, is_error, _payload = call_tool("move_file", {"src": src, "dst": dst})
        _s2, e2, stolen = call_tool("read_file", {"path": "mcp-stolen.txt"})
        ok = is_error and (e2 or not leaked(stolen)) and not (REPO / "mcp-planted.txt").exists()
        if ok:
            R.ok("mcp-escape", label, "-> tool error")
        else:
            R.bad("mcp-escape", label, f"is_error {is_error}, stolen leaked={leaked(stolen)}")

    # deleting a symlink must remove the LINK, never the target
    before = sha(secrets)
    call_tool("delete_file", {"path": "link-to-secret"})
    if secrets.exists() and sha(secrets) == before:
        R.ok("mcp-escape", "delete_file on a symlink spares its target")
    else:
        R.bad("mcp-escape", "delete_file on a symlink", "the target outside the folder was affected")

    call_tool("delete_file", {"path": "escape-dir", "recursive": True})
    still_there = (REPO / "secrets.env").exists() and (REPO / "src" / "app.py").exists()
    if still_there:
        R.ok("mcp-escape", "recursive delete_file through symlink spares the repo")
    else:
        R.bad("mcp-escape", "recursive delete_file through symlink", "DESTROYED FILES OUTSIDE THE FOLDER")


def main() -> int:
    print(f"{YELLOW}claude-files MCP connector containment suite{RESET}  mode={MODE}  target={BASE}")

    before = snapshot()
    functional()
    escapes()
    after = snapshot()

    print(f"\n{YELLOW}INTEGRITY -- nothing outside the shared folder changed{RESET}")
    if before == after:
        R.ok("integrity", f"{len(before)} paths outside shared/ are byte-identical")
    else:
        changed = {k for k in set(before) | set(after) if before.get(k) != after.get(k)}
        R.bad("integrity", "files outside shared/ changed", f"{sorted(changed)}")

    print(f"\n{'='*68}")
    if R.failed:
        print(f"{RED}FAILED{RESET}  {R.passed} passed, {R.failed} failed  (mode={MODE})")
        for f in R.failures:
            print(f"  {RED}x{RESET} {f}")
        return 1
    print(f"{GREEN}ALL PASSED{RESET}  {R.passed} checks  (mode={MODE})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
