#!/usr/bin/env python3
"""Test suite for bridge/claude_files_bridge.py.

Design brief: docs/feature-request-path-based-upload.md, "Tests". Imports
the bridge module directly and calls upload()/download()/check() -- no `mcp`
package required for tests 1-16, which is exactly why the bridge's core
functions take no Context (see bridge/claude_files_bridge.py's module
docstring). Test 17 is optional and skipped when `mcp` is not importable on
this runner.

Run against an already-running container (see tests/run_tests.sh, which
drives this the same way it drives tests/escape_suite.py and
tests/mcp_escape_suite.py -- against BOTH container configurations):

    BASE_URL=127.0.0.1:18080 API_TOKEN=... API_TOKEN_READONLY=... \\
      FIXTURE_REPO=... MODE=isolated python3 tests/bridge_tests.py
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import subprocess
import sys
import tracemalloc
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bridge"))

BASE = os.environ.get("BASE_URL", "127.0.0.1:18080")
TOKEN = os.environ["API_TOKEN"]
RO_TOKEN = os.environ.get("API_TOKEN_READONLY", "")
REPO = Path(os.environ["FIXTURE_REPO"])
MODE = os.environ.get("MODE", "unknown")

# The bridge reads its own config from these -- set here, once, the same way
# tests/mcp_escape_suite.py derives its own environment from the suite's.
os.environ["CLAUDE_FILES_URL"] = f"http://{BASE}"
os.environ["CLAUDE_FILES_TOKEN"] = TOKEN
os.environ.pop("CLAUDE_FILES_TOKEN_FILE", None)

import claude_files_bridge as bridge  # noqa: E402

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

TMP = Path(os.environ.get("RUNNER_TEMP", "/tmp")) / f"bridge-test-scratch-{os.getpid()}"
TMP.mkdir(parents=True, exist_ok=True)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def remote_file(name: str) -> Path:
    return REPO / "shared" / name


def rest_request(method: str, path: str, *, body: bytes | None = None,
                  token: str | None = TOKEN, headers: dict | None = None) -> tuple[int, bytes]:
    host, _, port = BASE.partition(":")
    conn = http.client.HTTPConnection(host, int(port or 80), timeout=30)
    hdrs = dict(headers or {})
    if token:
        hdrs["Authorization"] = f"Bearer {token}"
    try:
        conn.request(method, path, body=body, headers=hdrs)
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


def rest_put_chunked(path: str, chunks: list[bytes], *, token: str | None = TOKEN,
                      headers: dict | None = None) -> tuple[int, bytes]:
    """PUT via `Transfer-Encoding: chunked`, deliberately WITHOUT a
    Content-Length header -- the only way to reach _write_body's
    post-stream expect_bytes/expect_sha256 check from a well-behaved
    client, since any client that *does* declare Content-Length always
    declares the true length of what it sends, which the server catches
    earlier (before streaming) by comparing X-Expect-Bytes to
    Content-Length directly."""
    host, _, port = BASE.partition(":")
    conn = http.client.HTTPConnection(host, int(port or 80), timeout=30)
    hdrs = {"Transfer-Encoding": "chunked"}
    if token:
        hdrs["Authorization"] = f"Bearer {token}"
    hdrs.update(headers or {})
    try:
        conn.putrequest("PUT", path)
        for key, value in hdrs.items():
            conn.putheader(key, value)
        conn.endheaders()
        for chunk in chunks:
            conn.send(b"%x\r\n" % len(chunk) + chunk + b"\r\n")
        conn.send(b"0\r\n\r\n")
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


def mcp_call_tool(name: str, arguments: dict) -> dict:
    """A minimal JSON-RPC tools/call, just enough to read one tool's result
    (see tests/mcp_escape_suite.py's fuller version of the same thing)."""
    host, _, port = BASE.partition(":")
    conn = http.client.HTTPConnection(host, int(port or 80), timeout=30)
    body = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }).encode()
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    try:
        conn.request("POST", "/mcp", body=body, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
    finally:
        conn.close()
    parsed = json.loads(raw)
    result = parsed.get("result", {})
    content = result.get("content") or []
    text = content[0].get("text", "") if content else ""
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return {}


# ==========================================================================
# 1. Round trip, binary -- ~5x MCP_BASE64_LIMIT_BYTES, a size write_file
#    refuses outright.
# ==========================================================================

def test_round_trip_binary() -> None:
    print(f"\n{YELLOW}1. Round trip, binary (over the connector's base64 cap){RESET}")
    data = os.urandom(40 * 1024)
    local = TMP / "roundtrip-binary.bin"
    local.write_bytes(data)
    remote = "bridge-roundtrip-binary.bin"
    try:
        result = bridge.upload(str(local), remote)
    except Exception as exc:
        R.bad("bridge", "40KB binary upload", str(exc))
        return
    on_disk = remote_file(remote).read_bytes()
    if result.get("verified") and on_disk == data and result.get("sha256") == sha(data):
        R.ok("bridge", "40KB binary round trip, verified and byte-identical")
    else:
        R.bad("bridge", "40KB binary round trip",
              f"verified={result.get('verified')} equal={on_disk == data}")


# ==========================================================================
# 2/3. Round trip large (sized from the server, not a literal) + no memory
#      blow-up during the upload.
# ==========================================================================

def test_round_trip_large_and_memory() -> None:
    print(f"\n{YELLOW}2/3. Round trip large (sized from /v1/info) + no memory blow-up{RESET}")
    status, body = rest_request("GET", "/v1/info")
    if status != 200:
        R.bad("bridge", "fetch /v1/info for sizing", f"status {status}")
        return
    cap = json.loads(body)["max_upload_bytes"]
    size = int(cap * 0.75)
    data = os.urandom(size)
    local = TMP / "roundtrip-large.bin"
    local.write_bytes(data)
    remote = "bridge-roundtrip-large.bin"

    tracemalloc.start()
    try:
        result = bridge.upload(str(local), remote)
    except Exception as exc:
        tracemalloc.stop()
        R.bad("bridge", f"upload {size:,} bytes (75% of max_upload_bytes)", str(exc))
        return
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    on_disk = remote_file(remote).read_bytes()
    if result.get("verified") and on_disk == data:
        R.ok("bridge", f"{size:,}-byte round trip (75% of max_upload_bytes={cap:,})")
    else:
        R.bad("bridge", "large round trip", f"verified={result.get('verified')} equal={on_disk == data}")

    if peak < 4 * 1024 * 1024:
        R.ok("bridge", "peak traced memory during upload stays under 4 MiB", f"(peak={peak:,} bytes)")
    else:
        R.bad("bridge", "memory blow-up", f"peak allocation was {peak:,} bytes, wanted < 4 MiB")


# ==========================================================================
# 4. sha256 in the response
# ==========================================================================

def test_sha256_in_response() -> None:
    print(f"\n{YELLOW}4. sha256 in the response{RESET}")
    data = os.urandom(2048)
    local = TMP / "sha-check.bin"
    local.write_bytes(data)
    try:
        result = bridge.upload(str(local), "bridge-sha-check.bin")
    except Exception as exc:
        R.bad("bridge", "sha256 in response", str(exc))
        return
    if result.get("verified") is True and result.get("sha256") == sha(data):
        R.ok("bridge", "verified is True and sha256 matches the local digest")
    else:
        R.bad("bridge", "sha256 in response",
              f"verified={result.get('verified')} sha256={result.get('sha256')}")


# ==========================================================================
# 5/6/7. Integrity floor at the REST layer -- Part 2a.
# ==========================================================================

def test_expect_sha256_mismatch() -> None:
    print(f"\n{YELLOW}5. X-Expect-Sha256 mismatch refuses, nothing lands{RESET}")
    remote = "bridge-bad-sha.bin"
    dest = remote_file(remote)
    if dest.exists():
        dest.unlink()
    status, body = rest_request(
        "PUT", f"/v1/content/{remote}", body=b"hello world",
        headers={"Content-Length": "11", "X-Expect-Bytes": "11", "X-Expect-Sha256": "0" * 64},
    )
    parsed = json.loads(body) if body else {}
    if status == 422 and parsed.get("code") == "integrity_mismatch" and not dest.exists():
        R.ok("bridge", "wrong X-Expect-Sha256 -> 422 integrity_mismatch, nothing written")
    else:
        R.bad("bridge", "X-Expect-Sha256 mismatch",
              f"status {status} exists={dest.exists()} body={body[:200]!r}")


def test_expect_bytes_mismatch() -> None:
    print(f"\n{YELLOW}6. X-Expect-Bytes mismatch refuses{RESET}")
    remote = "bridge-bad-bytes.bin"
    dest = remote_file(remote)
    if dest.exists():
        dest.unlink()
    # Sent via chunked transfer encoding (no Content-Length) so the
    # mismatch is caught by the post-stream check on actual bytes written,
    # not the earlier Content-Length-vs-X-Expect-Bytes check -- see
    # rest_put_chunked's docstring.
    status, body = rest_put_chunked(
        f"/v1/content/{remote}", [b"hello world"], headers={"X-Expect-Bytes": "999"},
    )
    parsed = json.loads(body) if body else {}
    if status == 422 and parsed.get("code") == "integrity_mismatch" and not dest.exists():
        R.ok("bridge", "wrong X-Expect-Bytes -> 422 integrity_mismatch, nothing written")
    else:
        R.bad("bridge", "X-Expect-Bytes mismatch",
              f"status {status} exists={dest.exists()} body={body[:200]!r}")


def test_malformed_expect_sha256() -> None:
    print(f"\n{YELLOW}7. Malformed X-Expect-Sha256 -> 400, naming the header{RESET}")
    status, body = rest_request(
        "PUT", "/v1/content/bridge-malformed-sha.bin", body=b"x",
        headers={"Content-Length": "1", "X-Expect-Sha256": "not-a-hash"},
    )
    parsed = json.loads(body) if body else {}
    if status == 400 and "X-Expect-Sha256" in parsed.get("message", ""):
        R.ok("bridge", "malformed X-Expect-Sha256 -> 400, names the header")
    else:
        R.bad("bridge", "malformed X-Expect-Sha256", f"status {status} body={body[:200]!r}")


# ==========================================================================
# 8. Overwrite semantics
# ==========================================================================

def test_overwrite_semantics() -> None:
    print(f"\n{YELLOW}8. Overwrite semantics{RESET}")
    remote = "bridge-overwrite.bin"
    original = os.urandom(256)
    local1 = TMP / "overwrite-1.bin"
    local1.write_bytes(original)
    try:
        bridge.upload(str(local1), remote, overwrite=True)
    except Exception as exc:
        R.bad("bridge", "overwrite=True setup", str(exc))
        return

    local2 = TMP / "overwrite-2.bin"
    local2.write_bytes(os.urandom(256))
    try:
        bridge.upload(str(local2), remote, overwrite=False)
        R.bad("bridge", "overwrite=False onto an existing path", "did not raise")
    except bridge.ServerRefusal as exc:
        on_disk = remote_file(remote).read_bytes()
        if exc.status == 409 and on_disk == original:
            R.ok("bridge", "overwrite=False refuses an existing path (409), original untouched")
        else:
            R.bad("bridge", "overwrite=False", f"status={exc.status} untouched={on_disk == original}")
    except Exception as exc:
        R.bad("bridge", "overwrite=False", f"wrong exception type: {exc!r}")


# ==========================================================================
# 9. parents=True/False
# ==========================================================================

def test_parents() -> None:
    print(f"\n{YELLOW}9. parents=True creates the chain; parents=False -> 409 no_parent{RESET}")
    local = TMP / "parents.bin"
    local.write_bytes(b"parents test")

    remote = "bridge-parents/a/b/c.bin"
    try:
        bridge.upload(str(local), remote, parents=True)
        if remote_file(remote).exists():
            R.ok("bridge", "parents=True creates the missing chain")
        else:
            R.bad("bridge", "parents=True", "destination does not exist")
    except Exception as exc:
        R.bad("bridge", "parents=True", str(exc))

    remote2 = "bridge-no-parents-dir/deep.bin"
    try:
        bridge.upload(str(local), remote2, parents=False)
        R.bad("bridge", "parents=False into a missing directory", "did not raise")
    except bridge.ServerRefusal as exc:
        if exc.status == 409 and "no_parent" in str(exc).lower():
            R.ok("bridge", "parents=False into a missing directory -> 409 no_parent")
        else:
            R.bad("bridge", "parents=False", f"status={exc.status} -> {exc}")
    except Exception as exc:
        R.bad("bridge", "parents=False", f"wrong exception type: {exc!r}")


# ==========================================================================
# 10. Read-only token
# ==========================================================================

def test_readonly_token() -> None:
    print(f"\n{YELLOW}10. Read-only token: upload refused (403), download succeeds{RESET}")
    if not RO_TOKEN:
        print(f"  {DIM}SKIP -- no API_TOKEN_READONLY configured{RESET}")
        return

    seed = TMP / "readonly-seed.bin"
    seed.write_bytes(b"seed data for the read-only test")
    try:
        bridge.upload(str(seed), "bridge-readonly-seed.bin")
    except Exception as exc:
        R.bad("bridge", "read-only test setup", str(exc))
        return

    os.environ["CLAUDE_FILES_TOKEN"] = RO_TOKEN
    try:
        local = TMP / "readonly-upload.bin"
        local.write_bytes(b"should not land")
        try:
            bridge.upload(str(local), "bridge-readonly-should-not-exist.bin")
            R.bad("bridge", "read-only token upload", "did not raise")
        except bridge.ServerRefusal as exc:
            if exc.status == 403:
                R.ok("bridge", "read-only token: upload refused with 403")
            else:
                R.bad("bridge", "read-only token upload", f"status={exc.status}")
        except Exception as exc:
            R.bad("bridge", "read-only token upload", f"wrong exception type: {exc!r}")

        download_local = TMP / "readonly-download.bin"
        if download_local.exists():
            download_local.unlink()
        try:
            bridge.download("bridge-readonly-seed.bin", str(download_local))
            if download_local.read_bytes() == seed.read_bytes():
                R.ok("bridge", "read-only token: download succeeds")
            else:
                R.bad("bridge", "read-only token download", "content mismatch")
        except Exception as exc:
            R.bad("bridge", "read-only token download", str(exc))
    finally:
        os.environ["CLAUDE_FILES_TOKEN"] = TOKEN


# ==========================================================================
# 11. Traversal is refused, and nothing escapes
# ==========================================================================

def test_traversal_refused() -> None:
    print(f"\n{YELLOW}11. Traversal is refused, and nothing escapes{RESET}")
    outside = REPO / "secrets.env"
    before = outside.read_bytes() if outside.exists() else None
    local = TMP / "traversal.bin"
    local.write_bytes(b"PWNED_BY_BRIDGE_TEST")

    # remote_path is ALWAYS relative to the shared folder root, by design
    # (bridge/claude_files_bridge.py, "Remote path handling") -- a leading
    # '/' or an embedded '%2F' is not a traversal at this layer, it's just
    # an inert literal path component once segment-split and quoted. Only a
    # literal '.'/'..' segment or a NUL byte is refused client-side, before
    # any request; the other two are expected to land harmlessly INSIDE the
    # vault. What must hold for every one of them, refused or not, is that
    # nothing outside the shared folder is ever touched.
    strictly_refused = ["../escape.txt", "a\x00b"]
    all_refused = True
    for remote in strictly_refused:
        try:
            bridge.upload(str(local), remote)
            all_refused = False
            R.bad("bridge", f"traversal: {remote!r}", "upload did not raise")
        except bridge.UsageError:
            pass
        except Exception as exc:
            all_refused = False
            R.bad("bridge", f"traversal: {remote!r}", f"wrong exception type: {exc!r}")
    if all_refused:
        R.ok("bridge", "a literal '..' segment and a NUL byte are refused client-side (UsageError)")

    inert_inside_vault = ["..%2Fescape.txt", "/etc/cron.d/x"]
    for remote in inert_inside_vault:
        try:
            bridge.upload(str(local), remote)
        except Exception as exc:
            R.bad("bridge", f"inert path {remote!r}", f"unexpectedly raised: {exc!r}")

    after = outside.read_bytes() if outside.exists() else None
    if before == after:
        R.ok("bridge", "nothing outside the shared folder changed for any of the hostile paths")
    else:
        R.bad("bridge", "traversal integrity", "a file outside the shared folder changed")


# ==========================================================================
# 12. Local-side refusals
# ==========================================================================

def test_local_refusals() -> None:
    print(f"\n{YELLOW}12. Local-side refusals{RESET}")

    missing = TMP / "does-not-exist.bin"
    try:
        bridge.upload(str(missing), "bridge-missing-local.bin")
        R.bad("bridge", "missing local file", "did not raise")
    except bridge.LocalFileError:
        R.ok("bridge", "missing local file -> LocalFileError (exit code 2)")
    except Exception as exc:
        R.bad("bridge", "missing local file", f"wrong exception type: {exc!r}")

    directory = TMP / "a-directory"
    directory.mkdir(exist_ok=True)
    try:
        bridge.upload(str(directory), "bridge-dir-local.bin")
        R.bad("bridge", "local directory", "did not raise")
    except bridge.LocalFileError as exc:
        if "directory" in str(exc).lower():
            R.ok("bridge", "local directory -> LocalFileError naming 'directory'")
        else:
            R.bad("bridge", "local directory", f"message does not mention 'directory': {exc}")
    except Exception as exc:
        R.bad("bridge", "local directory", f"wrong exception type: {exc!r}")

    status, body = rest_request("GET", "/v1/info")
    cap = json.loads(body)["max_upload_bytes"]
    if cap > 200 * 1024 * 1024:
        print(f"  {DIM}SKIP -- max_upload_bytes={cap:,} too large to build a local oversize fixture{RESET}")
        return

    remote = "bridge-oversize-should-not-exist.bin"
    existed_before = remote_file(remote).exists()
    oversize = TMP / "oversize.bin"
    with open(oversize, "wb") as f:
        f.seek(cap)
        f.write(b"\0")
    try:
        bridge.upload(str(oversize), remote)
        R.bad("bridge", "oversize local file", "did not raise")
    except bridge.LocalFileError:
        if remote_file(remote).exists() == existed_before:
            R.ok("bridge", "oversize local file refused locally; no request reached the server")
        else:
            R.bad("bridge", "oversize local file", "a file was created on the server")
    except Exception as exc:
        R.bad("bridge", "oversize local file", f"wrong exception type: {exc!r}")
    finally:
        oversize.unlink(missing_ok=True)


# ==========================================================================
# 13. Download is atomic
# ==========================================================================

def test_download_atomic() -> None:
    print(f"\n{YELLOW}13. Download is atomic: a killed download leaves no file, no .part{RESET}")
    remote = "bridge-download-atomic.bin"
    data = os.urandom(64 * 1024)
    local_up = TMP / "download-atomic-src.bin"
    local_up.write_bytes(data)
    try:
        bridge.upload(str(local_up), remote)
    except Exception as exc:
        R.bad("bridge", "download atomicity setup", str(exc))
        return

    local_down = TMP / "download-atomic-dst.bin"
    local_down.unlink(missing_ok=True)
    part = Path(str(local_down) + ".part")

    real_urlopen = bridge.urllib.request.urlopen

    class _DyingResponse:
        """Wraps a real response, but raises after the first .read() call --
        simulating a connection dropped mid-stream."""

        def __init__(self, real_resp):
            self._real = real_resp
            self._reads = 0
            self.status = real_resp.status
            self.headers = real_resp.headers

        def read(self, n=-1):
            self._reads += 1
            if self._reads > 1:
                raise OSError("simulated mid-stream failure")
            return self._real.read(n)

        def close(self):
            self._real.close()

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            self.close()

    def _dying_urlopen(*a, **kw):
        return _DyingResponse(real_urlopen(*a, **kw))

    bridge.urllib.request.urlopen = _dying_urlopen
    try:
        try:
            bridge.download(remote, str(local_down))
            R.bad("bridge", "download atomicity", "download did not raise")
        except Exception:
            if not local_down.exists() and not part.exists():
                R.ok("bridge", "a killed download leaves no final file and no .part file")
            else:
                R.bad("bridge", "download atomicity",
                      f"local exists={local_down.exists()} part exists={part.exists()}")
    finally:
        bridge.urllib.request.urlopen = real_urlopen


# ==========================================================================
# 14. check() on a broken config
# ==========================================================================

def test_check_broken_config() -> None:
    print(f"\n{YELLOW}14. check() on a broken config{RESET}")
    old_token = os.environ.pop("CLAUDE_FILES_TOKEN", None)
    old_file = os.environ.pop("CLAUDE_FILES_TOKEN_FILE", None)
    try:
        result = bridge.check()
        remedy = result.get("remedy", "")
        if result.get("token_found") is False and "CLAUDE_FILES_TOKEN" in remedy:
            R.ok("bridge", "check() with no token names the missing variable, no traceback")
        else:
            R.bad("bridge", "check() broken config", f"{result!r}"[:300])
    except Exception as exc:
        R.bad("bridge", "check() broken config", f"raised instead of returning a result: {exc!r}")
    finally:
        if old_token is not None:
            os.environ["CLAUDE_FILES_TOKEN"] = old_token
        if old_file is not None:
            os.environ["CLAUDE_FILES_TOKEN_FILE"] = old_file


# ==========================================================================
# 15. GET /v1/bridge.py
# ==========================================================================

def test_bridge_py_route() -> None:
    print(f"\n{YELLOW}15. GET /v1/bridge.py{RESET}")
    status, body = rest_request("GET", "/v1/bridge.py", token=TOKEN)
    if status == 200 and body.startswith(b"#!"):
        R.ok("bridge", "/v1/bridge.py returns Python source starting with a shebang")
    else:
        R.bad("bridge", "/v1/bridge.py content", f"status {status} starts_with={body[:20]!r}")

    status_noauth, _ = rest_request("GET", "/v1/bridge.py", token=None)
    if status_noauth == 401:
        R.ok("bridge", "/v1/bridge.py refused without a token (401)")
    else:
        R.bad("bridge", "/v1/bridge.py auth", f"status {status_noauth}, wanted 401")

    status_vault, _ = rest_request("GET", "/v1/content/bridge.py")
    if status_vault == 404:
        R.ok("bridge", "GET /v1/content/bridge.py is a normal 404 -- served from the image, not the vault")
    else:
        R.bad("bridge", "/v1/content/bridge.py", f"status {status_vault}, wanted 404")


# ==========================================================================
# 16. /v1/info and get_folder_info report the bridge
# ==========================================================================

def test_discoverability() -> None:
    print(f"\n{YELLOW}16. /v1/info and get_folder_info report the bridge{RESET}")
    status, body = rest_request("GET", "/v1/info")
    info = json.loads(body) if status == 200 else {}
    if info.get("bridge_available") is True:
        R.ok("bridge", "/v1/info reports bridge_available: true")
    else:
        R.bad("bridge", "/v1/info bridge_available", f"status {status} -> {info!r}"[:200])

    folder_info = mcp_call_tool("get_folder_info", {})
    block = folder_info.get("bridge", {}) if isinstance(folder_info, dict) else {}
    if block.get("available") is True and block.get("note"):
        R.ok("bridge", "get_folder_info's bridge.note is present and non-empty")
    else:
        R.bad("bridge", "get_folder_info bridge block", f"{block!r}")


# ==========================================================================
# 17. (optional) a real stdio initialize + tools/list handshake, and the
#     stdout-discipline rule, tested rather than trusted.
# ==========================================================================

def test_serve_stdio() -> None:
    print(f"\n{YELLOW}17. (optional) stdio serve handshake{RESET}")
    try:
        import mcp  # noqa: F401
    except ImportError:
        print(f"  {DIM}SKIP -- 'mcp' package not installed on this runner{RESET}")
        return

    script = str(Path(__file__).resolve().parent.parent / "bridge" / "claude_files_bridge.py")
    env = dict(os.environ)

    def handshake(capture_all_stdout: bool):
        proc = subprocess.Popen(
            [sys.executable, script, "serve"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env, text=True, bufsize=1,
        )
        try:
            def send(msg: dict) -> None:
                proc.stdin.write(json.dumps(msg) + "\n")
                proc.stdin.flush()

            def recv() -> dict:
                line = proc.stdout.readline()
                return json.loads(line) if line.strip() else {}

            send({
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "bridge-test", "version": "0"},
                },
            })
            init_resp = recv()
            send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
            send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
            list_resp = recv()
            return init_resp, list_resp, proc
        except Exception:
            proc.kill()
            raise

    try:
        init_resp, list_resp, proc = handshake(capture_all_stdout=False)
    except Exception as exc:
        R.bad("bridge", "stdio handshake", f"could not complete handshake: {exc!r}")
        return
    finally:
        pass

    try:
        tools = {t["name"] for t in list_resp.get("result", {}).get("tools", [])}
        expected = {"upload_file", "download_file", "bridge_status"}
        if "result" in init_resp and tools == expected:
            R.ok("bridge", "stdio initialize + tools/list handshake exposes exactly 3 tools",
                 f"({sorted(tools)})")
        else:
            R.bad("bridge", "stdio handshake", f"init={init_resp!r} tools={tools!r}"[:300])
    finally:
        proc.stdin.close()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    # stdout discipline: nothing but JSON-RPC frames on stdout.
    proc2 = subprocess.Popen(
        [sys.executable, script, "serve"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        env=env, text=True, bufsize=1,
    )
    try:
        proc2.stdin.write(json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                       "clientInfo": {"name": "bridge-test", "version": "0"}},
        }) + "\n")
        proc2.stdin.flush()
        line = proc2.stdout.readline()
        clean = True
        if line.strip():
            try:
                json.loads(line)
            except json.JSONDecodeError:
                clean = False
        if clean:
            R.ok("bridge", "nothing but JSON-RPC frames appeared on stdout")
        else:
            R.bad("bridge", "stdout discipline", f"non-JSON on stdout: {line!r}")
    finally:
        proc2.stdin.close()
        try:
            proc2.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc2.kill()


def main() -> int:
    print(f"{YELLOW}claude-files bridge test suite{RESET}  mode={MODE}  target={BASE}")

    test_round_trip_binary()
    test_round_trip_large_and_memory()
    test_sha256_in_response()
    test_expect_sha256_mismatch()
    test_expect_bytes_mismatch()
    test_malformed_expect_sha256()
    test_overwrite_semantics()
    test_parents()
    test_readonly_token()
    test_traversal_refused()
    test_local_refusals()
    test_download_atomic()
    test_check_broken_config()
    test_bridge_py_route()
    test_discoverability()
    test_serve_stdio()

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
