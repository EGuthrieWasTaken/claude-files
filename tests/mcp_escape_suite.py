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

import base64
import hashlib
import http.client
import json
import os
import sys
import time
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
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", "2097152"))
UPLOAD_SESSION_TTL_SECONDS = int(os.environ.get("MCP_UPLOAD_SESSION_TTL_SECONDS", "900"))
MAX_CONCURRENT_UPLOADS = int(os.environ.get("MCP_MAX_CONCURRENT_UPLOADS", "8"))

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
        "copy_file", "begin_write", "write_chunk", "commit_write", "abort_write",
        "request_upload", "upload_status",
    }
    if expected <= tools:
        R.ok("func", "all expected tools are registered", f"({sorted(tools)})")
    else:
        R.bad("func", "tool registration", f"got {sorted(tools)}, missing {expected - tools}")

    status, is_err, info = call_tool("get_folder_info", {})
    if status == 200 and not is_err and info.get("token_scope") == "rw" \
            and info.get("writes_allowed") is True:
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
    if status == 200 and not _e2 and out2.get("text") == "replaced":
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

        _s, _e, payload = call_tool("write_file", {"path": "ro-should-fail.txt", "content": "x"},
                                    token=RO_TOKEN)
        if "read-write token" in str(payload) and "get_folder_info" in str(payload):
            R.ok("func", "read-only refusal tells the agent what to do next")
        else:
            R.bad("func", "read-only refusal message", f"{payload!r}"[:200])

        _s, is_err, info = call_tool("get_folder_info", {}, token=RO_TOKEN)
        if not is_err and info.get("token_scope") == "ro" and info.get("writes_allowed") is False:
            R.ok("func", "get_folder_info reports ro scope and writes_allowed=false")
        else:
            R.bad("func", "get_folder_info ro scope", f"{info!r}"[:200])

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
# INTEGRITY FLOOR -- docs/plan-mcp-large-writes.md, "Part 1"
# ==========================================================================

def integrity_floor() -> None:
    print(f"\n{YELLOW}INTEGRITY FLOOR -- write_file/read_file verification{RESET}")

    # 1. A base64 write_file truncated to a multiple of 4, with the
    #    *original* expect_bytes, is refused and no file is created. This is
    #    the regression test for the incident: a truncated base64 string
    #    that happens to have length%4==0 decodes "successfully" to fewer
    #    bytes than intended, and only expect_bytes/expect_sha256 catches it.
    original = os.urandom(600)
    full_b64 = base64.b64encode(original).decode("ascii")
    cut = len(full_b64) - (len(full_b64) % 4) - 4  # a shorter, still-valid-looking b64 string
    truncated_b64 = full_b64[:cut]
    assert len(truncated_b64) % 4 == 0
    status, is_err, out = call_tool("write_file", {
        "path": "mcp-truncated-regression.bin",
        "content": truncated_b64,
        "encoding": "base64",
        "expect_bytes": len(original),
    })
    status2, is_err2, out2 = call_tool("stat_file", {"path": "mcp-truncated-regression.bin"})
    if is_err and "truncated" in str(out).lower() and is_err2:
        R.ok("floor", "truncated base64 + correct expect_bytes refused, no file created")
    else:
        R.bad("floor", "truncation regression test",
              f"write is_err={is_err} ({str(out)[:150]!r}), file exists={not is_err2}")

    # 2. The same truncation with neither expect_* set, over the 1 KB
    #    threshold, is refused rather than silently writing short.
    big_original = os.urandom(2000)
    big_b64 = base64.b64encode(big_original).decode("ascii")
    big_cut = len(big_b64) - (len(big_b64) % 4) - 4
    status, is_err, out = call_tool("write_file", {
        "path": "mcp-truncated-noverify.bin",
        "content": big_b64[:big_cut],
        "encoding": "base64",
    })
    if is_err and "expect_bytes" in str(out):
        R.ok("floor", "base64 write over 1KB with neither expect_* is refused")
    else:
        R.bad("floor", "over-1KB no-verify refusal", f"is_err {is_err} -> {str(out)[:200]!r}")

    # 3. Round trip: read_file -> write_file forwarding the returned
    #    sha256/bytes succeeds and the destination is byte-identical.
    roundtrip_src = os.urandom(4096)
    call_tool("write_file", {
        "path": "mcp-roundtrip-src.bin",
        "content": base64.b64encode(roundtrip_src).decode("ascii"),
        "encoding": "base64",
        "expect_bytes": len(roundtrip_src),
    })
    status, is_err, src = call_tool("read_file", {"path": "mcp-roundtrip-src.bin", "encoding": "base64"})
    status, is_err, out = call_tool("write_file", {
        "path": "mcp-roundtrip-dst.bin",
        "content": src.get("content") if not is_err else "",
        "encoding": "base64",
        "expect_bytes": src.get("bytes") if not is_err else None,
        "expect_sha256": src.get("sha256") if not is_err else None,
    })
    src_bytes = (REPO / "shared" / "mcp-roundtrip-src.bin").read_bytes()
    dst_bytes = (REPO / "shared" / "mcp-roundtrip-dst.bin").read_bytes()
    if not is_err and src_bytes == dst_bytes:
        R.ok("floor", "read_file -> write_file round trip forwarding sha256/bytes is byte-identical")
    else:
        R.bad("floor", "read/write round trip", f"is_err {is_err}, equal={src_bytes == dst_bytes}")

    # 5. get_folder_info's advertised numbers and the values actually
    #    enforced come from the same config, asserted together.
    status, is_err, info = call_tool("get_folder_info", {})
    advertised_cap = info.get("mcp_base64_limit_bytes") if not is_err else None
    over = b"\xff" * ((advertised_cap or 0) + 1)
    status, is_err2, out = call_tool("write_file", {
        "path": "mcp-cap-drift-check.bin",
        "content": base64.b64encode(over).decode("ascii"),
        "encoding": "base64",
    })
    # The app formats the byte count with a thousands separator (f"{cap:,}"),
    # so match that rendering rather than the bare str(int).
    if (advertised_cap == BASE64_LIMIT_BYTES and is_err2
            and f"{advertised_cap:,}" in str(out)):
        R.ok("floor", "get_folder_info's advertised base64 cap matches what's enforced",
             f"(cap={advertised_cap})")
    else:
        R.bad("floor", "advertised vs enforced cap",
              f"advertised={advertised_cap}, configured={BASE64_LIMIT_BYTES}, enforced refusal={is_err2}")
    if "chunk_advisory" in str(info).lower() and "begin_write" in str(info):
        R.ok("floor", "get_folder_info mentions begin_write and a chunk advisory")
    else:
        R.bad("floor", "get_folder_info chunked-write mention", f"{str(info)[:200]!r}")


# ==========================================================================
# CHUNKED WRITES -- docs/plan-mcp-large-writes.md, "Part 2"
# ==========================================================================

def _chunk_b64(data: bytes, chunk_bytes: int = 3072) -> list[str]:
    """Split base64 on a 4-character boundary -- chunk_bytes must be a
    multiple of 3 so the resulting base64 slice length is a multiple of 4."""
    assert chunk_bytes % 3 == 0
    b64 = base64.b64encode(data).decode("ascii")
    step = chunk_bytes // 3 * 4
    return [b64[i:i + step] for i in range(0, len(b64), step)] or [""]


def _upload_chunked(data: bytes, path: str, *, encoding: str = "base64",
                     verify: str = "sha256") -> tuple[bool, Any]:
    status, is_err, out = call_tool("begin_write", {"path": path, "encoding": encoding})
    if is_err:
        return True, out
    upload_id = out["upload_id"]
    if encoding == "base64":
        chunks = _chunk_b64(data)
    else:
        text = data.decode("utf-8")
        chunks = [text[i:i + 2048] for i in range(0, len(text), 2048)] or [""]
    for seq, chunk in enumerate(chunks):
        status, is_err, out = call_tool("write_chunk", {
            "upload_id": upload_id, "seq": seq, "content": chunk,
        })
        if is_err:
            return True, out
    kwargs = {"upload_id": upload_id}
    if verify == "sha256":
        kwargs["expect_sha256"] = hashlib.sha256(data).hexdigest()
    elif verify == "bytes":
        kwargs["expect_bytes"] = len(data)
    status, is_err, out = call_tool("commit_write", kwargs)
    return is_err, out


def chunked_writes() -> None:
    print(f"\n{YELLOW}CHUNKED WRITES -- begin_write/write_chunk/commit_write/abort_write{RESET}")

    # 6. Happy path, both encodings, matching sha256.
    payload = os.urandom(20_000)
    is_err, out = _upload_chunked(payload, "mcp-chunked-binary.bin", encoding="base64")
    ok = not is_err and out.get("sha256") == hashlib.sha256(payload).hexdigest() \
        and (REPO / "shared" / "mcp-chunked-binary.bin").read_bytes() == payload
    R.ok("chunk", "base64 chunked upload round-trips byte-identical") if ok \
        else R.bad("chunk", "base64 chunked happy path", f"is_err {is_err} -> {str(out)[:200]!r}")

    text_payload = ("chunked text upload, séance café ☃ " * 500).encode("utf-8")
    is_err, out = _upload_chunked(text_payload, "mcp-chunked-text.txt", encoding="utf-8")
    ok = not is_err and (REPO / "shared" / "mcp-chunked-text.txt").read_bytes() == text_payload
    R.ok("chunk", "utf-8 chunked upload round-trips byte-identical") if ok \
        else R.bad("chunk", "utf-8 chunked happy path", f"is_err {is_err} -> {str(out)[:200]!r}")

    # 7. Ordering: skipped, repeated, out-of-order seq all refused; a resend
    #    after a failed call succeeds.
    status, is_err, out = call_tool("begin_write", {"path": "mcp-ordering.txt"})
    uid = out["upload_id"]
    _s, skip_err, _o = call_tool("write_chunk", {"upload_id": uid, "seq": 1, "content": "x"})
    _s, ok_err, _o = call_tool("write_chunk", {"upload_id": uid, "seq": 0, "content": "AB"})
    _s, repeat_err, _o = call_tool("write_chunk", {"upload_id": uid, "seq": 0, "content": "ZZ"})
    _s, resend_err, out = call_tool("write_chunk", {"upload_id": uid, "seq": 1, "content": "CD"})
    if skip_err and not ok_err and repeat_err and not resend_err:
        R.ok("chunk", "seq must equal next_seq exactly: skip/repeat refused, resend after retry ok")
    else:
        R.bad("chunk", "ordering",
              f"skip_err={skip_err} ok_err={ok_err} repeat_err={repeat_err} resend_err={resend_err}")
    call_tool("commit_write", {"upload_id": uid, "expect_bytes": 4})
    if (REPO / "shared" / "mcp-ordering.txt").read_bytes() == b"ABCD":
        R.ok("chunk", "ordering test content is exactly 'ABCD' (no dup, no gap)")
    else:
        R.bad("chunk", "ordering content",
              (REPO / "shared" / "mcp-ordering.txt").read_bytes() if (REPO / "shared" / "mcp-ordering.txt").exists() else "missing")

    # 8. A non-final base64 chunk not a multiple of 4 is refused.
    status, is_err, out = call_tool("begin_write", {"path": "mcp-boundary.bin", "encoding": "base64"})
    uid = out["upload_id"]
    _s, bad_err, out = call_tool("write_chunk", {"upload_id": uid, "seq": 0, "content": "abcde"})
    if bad_err and "multiple of 4" in str(out):
        R.ok("chunk", "non-multiple-of-4 base64 chunk is refused")
    else:
        R.bad("chunk", "base64 chunk boundary", f"is_err {bad_err} -> {str(out)[:200]!r}")
    call_tool("abort_write", {"upload_id": uid})

    # 9/10. commit_write refuses a wrong expect_sha256, a wrong expect_bytes,
    #       and neither -- each leaves no file at the destination.
    for label, kwargs, dest in [
        ("wrong expect_sha256", {"expect_sha256": "0" * 64}, "mcp-commit-badsha.bin"),
        ("wrong expect_bytes", {"expect_bytes": 999_999}, "mcp-commit-badbytes.bin"),
        ("neither expect_*", {}, "mcp-commit-noverify.bin"),
    ]:
        status, is_err, out = call_tool("begin_write", {"path": dest})
        uid = out["upload_id"] if not is_err else None
        if uid:
            call_tool("write_chunk", {"upload_id": uid, "seq": 0, "content": "hello"})
            _s, commit_err, _o = call_tool("commit_write", {"upload_id": uid, **kwargs})
            exists = (REPO / "shared" / dest).exists()
            if commit_err and not exists:
                R.ok("chunk", f"commit_write with {label} refused, no file created")
            else:
                R.bad("chunk", f"commit_write {label}", f"commit_err={commit_err} exists={exists}")
            call_tool("abort_write", {"upload_id": uid})

    # 12. abort_write mid-session leaves no temp file and no destination;
    #     calling it twice, and on an unknown id, both succeed quietly.
    status, is_err, out = call_tool("begin_write", {"path": "mcp-abort-me.bin"})
    uid = out["upload_id"]
    call_tool("write_chunk", {"upload_id": uid, "seq": 0, "content": "partial"})
    tmp_before = list((REPO / "shared").glob(".upload-*.tmp"))
    _s, e1, out1 = call_tool("abort_write", {"upload_id": uid})
    _s, e2, out2 = call_tool("abort_write", {"upload_id": uid})
    _s, e3, out3 = call_tool("abort_write", {"upload_id": "not-a-real-upload-id"})
    tmp_after = list((REPO / "shared").glob(".upload-*.tmp"))
    dest_missing = not (REPO / "shared" / "mcp-abort-me.bin").exists()
    if (not e1 and out1.get("aborted") is True and not e2 and out2.get("aborted") is False
            and not e3 and out3.get("aborted") is False and dest_missing
            and len(tmp_after) < len(tmp_before) + 1):
        R.ok("chunk", "abort_write cleans up; idempotent; safe on an unknown id")
    else:
        R.bad("chunk", "abort_write",
              f"{out1!r} {out2!r} {out3!r} dest_missing={dest_missing} tmp before/after={len(tmp_before)}/{len(tmp_after)}")

    # 13. Cumulative MAX_UPLOAD_BYTES is enforced during chunking, not only
    #     at commit. Skipped when the configured cap is too large to reach in
    #     a reasonable number of chunks (a production-sized default).
    if MAX_UPLOAD_BYTES <= 8 * 1024 * 1024:
        status, is_err, out = call_tool("begin_write", {"path": "mcp-over-max.bin", "encoding": "base64"})
        uid = out["upload_id"]
        chunk_raw_bytes = 98304  # 96 KiB, a multiple of 3 -> clean base64 chunk boundary
        seq = 0
        refused = False
        refusal_text = ""
        max_chunks = MAX_UPLOAD_BYTES // chunk_raw_bytes + 4  # enough to exceed the cap
        while seq < max_chunks:
            chunk = base64.b64encode(os.urandom(chunk_raw_bytes)).decode("ascii")
            _s, is_err, out = call_tool("write_chunk", {"upload_id": uid, "seq": seq, "content": chunk})
            if is_err:
                refused = True
                refusal_text = str(out)
                break
            seq += 1
        call_tool("abort_write", {"upload_id": uid})
        if refused and f"{MAX_UPLOAD_BYTES:,}" in refusal_text:
            R.ok("chunk", "cumulative MAX_UPLOAD_BYTES enforced during chunking, names the limit")
        else:
            R.bad("chunk", "cumulative cap", f"refused={refused} after {seq} chunks -> {refusal_text[:200]!r}")
    else:
        print(f"  {DIM}SKIP  [chunk] cumulative MAX_UPLOAD_BYTES test -- {MAX_UPLOAD_BYTES:,} is too "
              f"large to reach in a reasonable number of chunks{RESET}")

    # 14. MCP_MAX_CONCURRENT_UPLOADS is enforced; message names abort_write.
    opened = []
    hit_cap = False
    cap_text = ""
    for i in range(MAX_CONCURRENT_UPLOADS + 3):
        status, is_err, out = call_tool("begin_write", {"path": f"mcp-concurrency-{i}.bin"})
        if is_err:
            hit_cap = True
            cap_text = str(out)
            break
        opened.append(out["upload_id"])
    for uid in opened:
        call_tool("abort_write", {"upload_id": uid})
    if hit_cap and "abort_write" in cap_text:
        R.ok("chunk", "MCP_MAX_CONCURRENT_UPLOADS enforced, message names abort_write",
             f"(after {len(opened)} open)")
    else:
        R.bad("chunk", "concurrency cap", f"hit_cap={hit_cap} opened={len(opened)} -> {cap_text[:200]!r}")

    # 16. A session opened with one token cannot be chunked or committed
    #     with another.
    if RO_TOKEN:
        status, is_err, out = call_tool("begin_write", {"path": "mcp-cross-token.bin"})
        uid = out["upload_id"]
        _s, cross_err, cross_out = call_tool(
            "write_chunk", {"upload_id": uid, "seq": 0, "content": "x"}, token=RO_TOKEN,
        )
        if cross_err:
            R.ok("chunk", "write_chunk with a different token than begin_write is refused")
        else:
            R.bad("chunk", "cross-token write_chunk", f"{cross_out!r}"[:200])
        call_tool("abort_write", {"upload_id": uid})

    # 18. Atomicity: the destination does not exist at any point before
    #     commit_write returns -- poll between chunks.
    dest = "mcp-atomicity.bin"
    status, is_err, out = call_tool("begin_write", {"path": dest})
    uid = out["upload_id"]
    saw_partial = False
    for seq in range(5):
        call_tool("write_chunk", {"upload_id": uid, "seq": seq, "content": "x" * 100})
        if (REPO / "shared" / dest).exists():
            saw_partial = True
    call_tool("commit_write", {"upload_id": uid, "expect_bytes": 500})
    if not saw_partial and (REPO / "shared" / dest).exists():
        R.ok("chunk", "destination never exists before commit_write, exists after")
    else:
        R.bad("chunk", "atomicity", f"saw_partial={saw_partial}")

    # 15. Read-only token: all four chunked-write tools refused, including
    #     abort_write.
    if RO_TOKEN:
        status, is_err, out = call_tool("begin_write", {"path": "mcp-ro-should-fail.bin"}, token=RO_TOKEN)
        ro_begin_refused = is_err and "read-only" in str(out)
        status, is_err, out = call_tool("begin_write", {"path": "mcp-ro-setup.bin"})
        uid = out["upload_id"] if not is_err else None
        ro_chunk_refused = ro_commit_refused = ro_abort_refused = False
        if uid:
            _s, e, o = call_tool("write_chunk", {"upload_id": uid, "seq": 0, "content": "x"}, token=RO_TOKEN)
            ro_chunk_refused = e and "read-only" in str(o)
            _s, e, o = call_tool("commit_write", {"upload_id": uid, "expect_bytes": 1}, token=RO_TOKEN)
            ro_commit_refused = e and "read-only" in str(o)
            _s, e, o = call_tool("abort_write", {"upload_id": uid}, token=RO_TOKEN)
            ro_abort_refused = e and "read-only" in str(o)
            call_tool("abort_write", {"upload_id": uid})
        if ro_begin_refused and ro_chunk_refused and ro_commit_refused and ro_abort_refused:
            R.ok("chunk", "read-only token refuses all four chunked-write tools")
        else:
            R.bad("chunk", "read-only chunked-write tools",
                  f"begin={ro_begin_refused} chunk={ro_chunk_refused} "
                  f"commit={ro_commit_refused} abort={ro_abort_refused}")

    # 11. Abandonment: a session left open past its TTL is reaped -- its temp
    #     file is gone, the destination doesn't exist, and the upload_id is
    #     unknown afterwards. Only meaningful when the container was started
    #     with a short TTL (tests/run_tests.sh sets one); skip the wait on a
    #     production-sized default rather than sleep 900s.
    if UPLOAD_SESSION_TTL_SECONDS <= 30:
        status, is_err, out = call_tool("begin_write", {"path": "mcp-abandoned.bin"})
        uid = out["upload_id"]
        call_tool("write_chunk", {"upload_id": uid, "seq": 0, "content": "orphaned"})
        tmp_before = {p.name for p in (REPO / "shared").glob(".upload-*.tmp")}
        time.sleep(UPLOAD_SESSION_TTL_SECONDS + 2)
        # Reaping is lazy -- triggered by the next begin_write/write_chunk. This
        # call opens its own session (with its own new temp file), so compare
        # by name -- tmp_before's specific file(s) must be gone -- rather than
        # by count, which a freshly-added temp file would keep flat.
        status2, is_err2, out2 = call_tool("begin_write", {"path": "mcp-reap-trigger.bin"})
        tmp_after = {p.name for p in (REPO / "shared").glob(".upload-*.tmp")}
        _s, unknown_err, _o = call_tool("write_chunk", {"upload_id": uid, "seq": 1, "content": "x"})
        dest_missing = not (REPO / "shared" / "mcp-abandoned.bin").exists()
        if unknown_err and dest_missing and not (tmp_before & tmp_after):
            R.ok("chunk", "an idle session past its TTL is reaped (temp file gone, upload_id unknown)")
        else:
            R.bad("chunk", "TTL reap",
                  f"unknown_err={unknown_err} dest_missing={dest_missing} "
                  f"tmp before={sorted(tmp_before)} after={sorted(tmp_after)}")
        if not is_err2:
            call_tool("abort_write", {"upload_id": out2["upload_id"]})
    else:
        print(f"  {DIM}SKIP  [chunk] TTL reap test -- MCP_UPLOAD_SESSION_TTL_SECONDS="
              f"{UPLOAD_SESSION_TTL_SECONDS} is too long to wait out{RESET}")


# ==========================================================================
# COPY -- copy_file (docs/plan-mcp-large-writes.md, "Considered and not
# chosen" A, added anyway as the cheap, non-conflicting follow-on)
# ==========================================================================

def copy_tests() -> None:
    print(f"\n{YELLOW}COPY -- copy_file{RESET}")

    payload = os.urandom(5000)
    call_tool("write_file", {
        "path": "mcp-copy-src.bin",
        "content": base64.b64encode(payload).decode("ascii"),
        "encoding": "base64",
        "expect_bytes": len(payload),
    })
    status, is_err, out = call_tool("copy_file", {"src": "mcp-copy-src.bin", "dst": "mcp-copy-dst.bin"})
    same = (REPO / "shared" / "mcp-copy-src.bin").read_bytes() == (REPO / "shared" / "mcp-copy-dst.bin").read_bytes() \
        if not is_err else False
    if not is_err and same and out.get("size") == len(payload):
        R.ok("copy", "copy_file produces a byte-identical copy")
    else:
        R.bad("copy", "copy_file basic", f"is_err {is_err} -> {str(out)[:200]!r}")

    status, is_err, out = call_tool("copy_file", {"src": "mcp-copy-src.bin", "dst": "mcp-copy-dst.bin"})
    if is_err:
        R.ok("copy", "copy_file overwrite=false refuses an existing destination")
    else:
        R.bad("copy", "copy_file overwrite=false", f"{out!r}"[:200])

    status, is_err, out = call_tool("copy_file", {
        "src": "mcp-copy-src.bin", "dst": "mcp-copy-deep/a/b.bin", "parents": True,
    })
    if not is_err and (REPO / "shared" / "mcp-copy-deep" / "a" / "b.bin").read_bytes() == payload:
        R.ok("copy", "copy_file parents=true creates the destination chain")
    else:
        R.bad("copy", "copy_file parents", f"is_err {is_err} -> {str(out)[:200]!r}")

    status, is_err, out = call_tool("copy_file", {"src": "mcp-copy-nonexistent.bin", "dst": "mcp-copy-x.bin"})
    if is_err:
        R.ok("copy", "copy_file refuses a nonexistent source")
    else:
        R.bad("copy", "copy_file missing source", f"{out!r}"[:200])

    if RO_TOKEN:
        status, is_err, out = call_tool(
            "copy_file", {"src": "mcp-copy-src.bin", "dst": "mcp-copy-ro.bin"}, token=RO_TOKEN,
        )
        if is_err and "read-only" in str(out):
            R.ok("copy", "read-only token refuses copy_file")
        else:
            R.bad("copy", "read-only copy_file", f"is_err {is_err} -> {str(out)[:200]!r}")


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

    # copy_file must not read from or write to outside the shared folder
    for label, src, dst in [
        ("copy src outside", "../secrets.env", "mcp-copy-stolen.txt"),
        ("copy dst outside", "existing.txt", "../mcp-copy-planted.txt"),
        ("copy via symlinked src", "escape-dir/secrets.env", "mcp-copy-stolen.txt"),
        ("copy via symlinked dst dir", "existing.txt", "escape-dir/mcp-copy-planted.txt"),
    ]:
        status, is_error, _payload = call_tool("copy_file", {"src": src, "dst": dst})
        _s2, e2, stolen = call_tool("read_file", {"path": "mcp-copy-stolen.txt"})
        ok = (is_error and (e2 or not leaked(stolen))
              and not (REPO / "mcp-copy-planted.txt").exists())
        if ok:
            R.ok("mcp-escape", f"copy_file: {label}", "-> tool error")
        else:
            R.bad("mcp-escape", f"copy_file: {label}", f"is_error {is_error}, stolen leaked={leaked(stolen)}")

    # begin_write must never open a session against a hostile path -- no
    # temp file, no upload_id, same containment as single-shot write_file.
    for name, path in [
        ("write through ..",        "../planted-mcp-begin.txt"),
        ("write through symlink",   "link-to-secret"),
        ("write via symlinked dir", "escape-dir/secrets.env"),
        ("write into .git",         ".git/hooks/evil-begin"),
        ("absolute path",           "/etc/passwd"),
    ]:
        before_hash = sha(secrets)
        status, is_error, payload = call_tool("begin_write", {"path": path})
        if not is_error:
            # A session was opened against a path that should have been
            # refused -- abort it immediately so it can't leave a dangling
            # fd, then fail the check either way.
            call_tool("abort_write", {"upload_id": payload.get("upload_id", "")})
        after_hash = sha(secrets)
        if is_error and before_hash == after_hash:
            R.ok("mcp-escape", f"begin_write: {name}", "-> tool error")
        else:
            R.bad("mcp-escape", f"begin_write: {name}",
                  f"is_error {is_error}, secrets touched={before_hash != after_hash}")

    # deleting a symlink must remove the LINK, never the target. Uses its own
    # dedicated symlinks (not link-to-secret/escape-dir) so this doesn't
    # consume the read/write escape targets this suite's own tests above --
    # and escape_suite.py's, when run_tests.sh runs it against this same
    # fixture first -- still need.
    before = sha(secrets)
    call_tool("delete_file", {"path": "link-to-secret-del"})
    if secrets.exists() and sha(secrets) == before:
        R.ok("mcp-escape", "delete_file on a symlink spares its target")
    else:
        R.bad("mcp-escape", "delete_file on a symlink", "the target outside the folder was affected")

    call_tool("delete_file", {"path": "escape-dir-del", "recursive": True})
    still_there = (REPO / "secrets.env").exists() and (REPO / "src" / "app.py").exists()
    if still_there:
        R.ok("mcp-escape", "recursive delete_file through symlink spares the repo")
    else:
        R.bad("mcp-escape", "recursive delete_file through symlink", "DESTROYED FILES OUTSIDE THE FOLDER")


# ==========================================================================
# UPLOAD URLS -- request_upload / upload_status (docs/upload-urls.md)
# ==========================================================================

def _put_url(url: str, body: bytes) -> tuple[int, Any]:
    """PUT to a ticket's upload_url with no Authorization header, as curl would."""
    from urllib.parse import urlsplit
    u = urlsplit(url)
    conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=30)
    try:
        conn.request("PUT", u.path, body=body, headers={"Content-Type": "application/octet-stream"})
        resp = conn.getresponse()
        raw = resp.read()
        try:
            return resp.status, json.loads(raw)
        except json.JSONDecodeError:
            return resp.status, raw
    finally:
        conn.close()


def upload_url_tests() -> None:
    print(f"\n{YELLOW}UPLOAD URLS -- request_upload / upload_status{RESET}")

    payload = os.urandom(300_000)
    digest = hashlib.sha256(payload).hexdigest()
    _s, is_err, t = call_tool("request_upload", {"path": "mcp-ticket/deep/file.bin"})
    if is_err or not isinstance(t, dict) or "upload_url" not in t:
        R.bad("upload", "request_upload issues a ticket", f"{t!r}"[:200])
        return
    R.ok("upload", "request_upload issues a ticket with upload_url and curl_command") \
        if "<LOCAL_FILE>" in t.get("curl_command", "") \
        else R.bad("upload", "curl_command", f"{t.get('curl_command')!r}")

    _s, is_err, st = call_tool("upload_status", {"upload_id": t["upload_id"]})
    R.ok("upload", "upload_status reports pending before the PUT") \
        if not is_err and st.get("state") == "pending" \
        else R.bad("upload", "pending status", f"{st!r}"[:200])

    status, body = _put_url(t["upload_url"], payload)
    on_disk = REPO / "shared" / "mcp-ticket" / "deep" / "file.bin"
    if status == 200 and isinstance(body, dict) and body.get("sha256") == digest \
            and on_disk.read_bytes() == payload:
        R.ok("upload", "300 KB binary via upload_url lands byte-identical, sha256 returned")
    else:
        R.bad("upload", "PUT to upload_url", f"status {status} -> {str(body)[:200]!r}")

    _s, is_err, st = call_tool("upload_status", {"upload_id": t["upload_id"]})
    R.ok("upload", "upload_status reports complete with matching sha256") \
        if not is_err and st.get("state") == "complete" and st.get("sha256") == digest \
        and st.get("size") == len(payload) \
        else R.bad("upload", "complete status", f"{st!r}"[:200])

    status, _b = _put_url(t["upload_url"], b"again")
    R.ok("upload", "reusing the upload_url -> 409") if status == 409 \
        else R.bad("upload", "reuse", f"status {status}")

    _s, is_err, t2 = call_tool("request_upload", {
        "path": "mcp-ticket/checked.txt", "expect_sha256": "0" * 64,
    })
    if not is_err:
        status, _b = _put_url(t2["upload_url"], b"not matching")
        exists = (REPO / "shared" / "mcp-ticket" / "checked.txt").exists()
        R.ok("upload", "expect_sha256 mismatch -> 422, nothing written") \
            if status == 422 and not exists \
            else R.bad("upload", "expect_sha256 mismatch", f"status {status} exists {exists}")
    else:
        R.bad("upload", "request_upload with expect_sha256", f"{t2!r}"[:200])

    _s, is_err, out = call_tool("request_upload", {
        "path": "mcp-ticket/deep/file.bin", "overwrite": False,
    })
    R.ok("upload", "overwrite=false onto an existing file refused at request time") \
        if is_err and "exists" in str(out) \
        else R.bad("upload", "overwrite=false", f"{out!r}"[:200])

    _s, is_err, out = call_tool("upload_status", {"upload_id": "u_nope"})
    R.ok("upload", "upload_status on an unknown id is an error") if is_err \
        else R.bad("upload", "unknown id", f"{out!r}"[:200])

    if RO_TOKEN:
        _s, is_err, out = call_tool("request_upload", {"path": "mcp-ticket/ro.bin"}, token=RO_TOKEN)
        R.ok("upload", "read-only token refuses request_upload") \
            if is_err and "read-only" in str(out) \
            else R.bad("upload", "ro request_upload", f"{out!r}"[:200])

    for label, path in [
        ("traversal", "../outside.txt"),
        ("absolute", "/etc/passwd"),
        (".git component", ".git/config"),
        ("symlinked parent", "escape-dir/escape.txt"),
    ]:
        _s, is_err, out = call_tool("request_upload", {"path": path})
        if is_err and not leaked(out):
            R.ok("mcp-escape", f"request_upload refuses {label} path")
        else:
            R.bad("mcp-escape", f"request_upload {label}", f"{out!r}"[:200])


def main() -> int:
    print(f"{YELLOW}claude-files MCP connector containment suite{RESET}  mode={MODE}  target={BASE}")

    before = snapshot()
    functional()
    integrity_floor()
    chunked_writes()
    copy_tests()
    upload_url_tests()
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
