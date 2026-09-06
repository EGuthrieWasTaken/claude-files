#!/usr/bin/env python3
"""Containment test suite for claude-files.

Two kinds of tests:

  FUNCTIONAL  -- the API does what it claims inside the shared folder.
  ESCAPE      -- every attempt to reach outside the shared folder fails.

Escape tests are written to be genuinely hostile: raw `..` in the request
line (http.client does not normalise it the way curl does), percent-encoded
and double-encoded traversal, absolute paths, NUL bytes, and reads/writes
through symlinks and hard links that were planted on the host inside the
shared folder.

Pass/fail for an escape test is not "did it return an error" -- it is
"did the canary string stay out of the response, and did the file outside
the shared folder stay byte-identical".
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

BASE = os.environ.get("BASE_URL", "127.0.0.1:18080")
TOKEN = os.environ["API_TOKEN"]
RO_TOKEN = os.environ.get("API_TOKEN_READONLY", "")
REPO = Path(os.environ["FIXTURE_REPO"])
MODE = os.environ.get("MODE", "unknown")

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


def request(method: str, path: str, *, body=None, token: str | None = TOKEN,
            headers: dict | None = None) -> tuple[int, dict, bytes]:
    """Send a request with the path EXACTLY as given -- no normalisation."""
    host, _, port = BASE.partition(":")
    conn = http.client.HTTPConnection(host, int(port or 80), timeout=30)
    hdrs = dict(headers or {})
    if token:
        hdrs["Authorization"] = f"Bearer {token}"
    try:
        conn.request(method, path, body=body, headers=hdrs)
        resp = conn.getresponse()
        data = resp.read()
        return resp.status, dict(resp.getheaders()), data
    finally:
        conn.close()


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def snapshot() -> dict[str, str]:
    """Hash every file outside the shared folder, plus the shared folder's own
    tree shape, so we can prove nothing outside changed."""
    out = {}
    for p in sorted(REPO.rglob("*")):
        if "shared" in p.relative_to(REPO).parts:
            continue
        if p.is_file() and not p.is_symlink():
            out[str(p.relative_to(REPO))] = sha(p)
        elif p.is_dir():
            out[str(p.relative_to(REPO)) + "/"] = "dir"
    return out


# ==========================================================================
# FUNCTIONAL
# ==========================================================================

def functional() -> None:
    print(f"\n{YELLOW}FUNCTIONAL -- the API works inside the shared folder{RESET}")

    st, _, body = request("GET", "/health", token=None)
    R.ok("func", "health needs no auth") if st == 200 else R.bad("func", "health", f"status {st}")

    st, _, _ = request("GET", "/v1/list", token=None)
    R.ok("func", "no token -> 401") if st == 401 else R.bad("func", "no token", f"status {st}")

    st, _, _ = request("GET", "/v1/list", token="wrong-token-0123456789abcdef")
    R.ok("func", "bad token -> 401") if st == 401 else R.bad("func", "bad token", f"status {st}")

    payload = b"agent wrote this\n" * 100
    st, _, body = request("PUT", "/v1/content/report.txt", body=payload)
    if st == 200 and json.loads(body)["size"] == len(payload):
        R.ok("func", "PUT creates a file")
    else:
        R.bad("func", "PUT creates a file", f"status {st} body {body[:200]!r}")

    st, _, body = request("GET", "/v1/content/report.txt")
    if st == 200 and body == payload:
        R.ok("func", "GET round-trips the exact bytes")
    else:
        R.bad("func", "GET round-trip", f"status {st}, {len(body)} bytes")

    st, _, _ = request("PUT", "/v1/content/report.txt", body=b"replaced")
    st2, _, body2 = request("GET", "/v1/content/report.txt")
    if st == 200 and body2 == b"replaced":
        R.ok("func", "PUT replaces an existing file")
    else:
        R.bad("func", "PUT replace", f"status {st}/{st2} body {body2[:80]!r}")

    st, _, _ = request("POST", "/v1/content/once.txt", body=b"first")
    st2, _, _ = request("POST", "/v1/content/once.txt", body=b"second")
    if st == 200 and st2 == 409:
        R.ok("func", "POST is create-only (409 on repeat)")
    else:
        R.bad("func", "POST create-only", f"got {st} then {st2}, wanted 200 then 409")

    st, _, _ = request("POST", "/v1/mkdir/a/b/c")
    st2, _, _ = request("PUT", "/v1/content/a/b/c/deep.txt", body=b"deep")
    if st == 200 and st2 == 200:
        R.ok("func", "mkdir -p then write into it")
    else:
        R.bad("func", "mkdir", f"got {st}/{st2}")

    st, _, body = request("GET", "/v1/list")
    names = {e["name"] for e in json.loads(body)["entries"]} if st == 200 else set()
    if st == 200 and {"report.txt", "once.txt", "a", "existing.txt"} <= names:
        R.ok("func", "list shows created entries", f"({len(names)} entries)")
    else:
        R.bad("func", "list", f"status {st}, names {sorted(names)}")

    st, _, body = request("GET", "/v1/list?recursive=true")
    paths = {e["path"] for e in json.loads(body)["entries"]} if st == 200 else set()
    if "a/b/c/deep.txt" in paths:
        R.ok("func", "recursive list descends")
    else:
        R.bad("func", "recursive list", f"status {st}, paths {sorted(paths)[:10]}")

    st, _, body = request("GET", "/v1/stat/report.txt")
    if st == 200 and json.loads(body)["type"] == "file":
        R.ok("func", "stat reports type")
    else:
        R.bad("func", "stat", f"status {st} body {body[:120]!r}")

    st, _, _ = request("POST", "/v1/move", body=json.dumps(
        {"src": "once.txt", "dst": "a/moved.txt"}).encode(),
        headers={"Content-Type": "application/json"})
    st2, _, _ = request("GET", "/v1/content/a/moved.txt")
    if st == 200 and st2 == 200:
        R.ok("func", "move within the folder")
    else:
        R.bad("func", "move", f"got {st}/{st2}")

    st, _, _ = request("DELETE", "/v1/content/report.txt")
    st2, _, _ = request("GET", "/v1/content/report.txt")
    if st == 200 and st2 == 404:
        R.ok("func", "DELETE removes a file")
    else:
        R.bad("func", "delete file", f"got {st}/{st2}")

    st, _, _ = request("DELETE", "/v1/content/a")
    st2, _, _ = request("DELETE", "/v1/content/a?recursive=true")
    if st == 409 and st2 == 200:
        R.ok("func", "non-empty dir needs recursive=true")
    else:
        R.bad("func", "recursive delete", f"got {st}/{st2}, wanted 409 then 200")

    big = b"x" * (2 * 1024 * 1024 + 1)
    st, _, _ = request("PUT", "/v1/content/toobig.bin", body=big)
    if st == 413:
        R.ok("func", "oversize upload -> 413")
    else:
        R.bad("func", "size limit", f"status {st}, wanted 413")

    if RO_TOKEN:
        st, _, _ = request("GET", "/v1/list", token=RO_TOKEN)
        st2, _, _ = request("PUT", "/v1/content/ro.txt", body=b"x", token=RO_TOKEN)
        st3, _, _ = request("DELETE", "/v1/content/existing.txt", token=RO_TOKEN)
        if st == 200 and st2 == 403 and st3 == 403:
            R.ok("func", "read-only token cannot write or delete")
        else:
            R.bad("func", "read-only token", f"got {st}/{st2}/{st3}, wanted 200/403/403")


# ==========================================================================
# ESCAPE
# ==========================================================================

def leaked(body: bytes) -> str | None:
    for c in CANARIES:
        if c in body:
            return c.decode()
    return None


def escape_read(name: str, path: str) -> None:
    """A read that must not return anything from outside the shared folder."""
    try:
        st, _, body = request("GET", path)
    except Exception as exc:                      # a hard connection error is a pass
        R.ok("escape", name, f"({type(exc).__name__})")
        return
    hit = leaked(body)
    if hit:
        R.bad("escape", name, f"LEAKED {hit!r} (status {st})")
    elif st == 200:
        R.bad("escape", name, f"returned 200 with {len(body)} bytes")
    else:
        R.ok("escape", name, f"-> {st}")


def escape_write(name: str, path: str, victim: Path, method: str = "PUT") -> None:
    """A write that must not modify anything outside the shared folder."""
    before = sha(victim) if victim.exists() else None
    try:
        st, _, _ = request(method, path, body=b"PWNED_BY_TEST")
    except Exception as exc:
        st = -1
        _ = exc
    after = sha(victim) if victim.exists() else None
    if before != after:
        R.bad("escape", name, f"MODIFIED {victim}")
    elif st == 200:
        R.bad("escape", name, "write returned 200")
    else:
        R.ok("escape", name, f"-> {st}")


def escapes() -> None:
    print(f"\n{YELLOW}ESCAPE -- these must all fail{RESET}")

    secrets = REPO / "secrets.env"

    # --- traversal in the request line -----------------------------------
    escape_read("raw ..",                    "/v1/content/../secrets.env")
    escape_read("raw ../..",                 "/v1/content/../../etc/passwd")
    escape_read("deep ../ chain",            "/v1/content/../../../../../../etc/passwd")
    escape_read("traversal mid-path",        "/v1/content/nested/../../secrets.env")
    escape_read("percent-encoded ..%2f",     "/v1/content/..%2fsecrets.env")
    escape_read("fully encoded traversal",   "/v1/content/%2e%2e%2f%2e%2e%2fetc%2fpasswd")
    escape_read("double-encoded traversal",  "/v1/content/%252e%252e%252fsecrets.env")
    escape_read("overlong utf-8 dots",       "/v1/content/%c0%ae%c0%ae%2fsecrets.env")
    escape_read("dot-dot-slash padding",     "/v1/content/....//secrets.env")
    escape_read("dot segment",               "/v1/content/./../secrets.env")
    escape_read("absolute path",             "/v1/content//etc/passwd")
    escape_read("absolute via encoding",     "/v1/content/%2fetc%2fpasswd")
    escape_read("backslash traversal",       "/v1/content/..%5c..%5csecrets.env")
    escape_read("NUL byte truncation",       "/v1/content/existing.txt%00../../secrets.env")
    escape_read("traversal in list query",   "/v1/list?path=..")
    escape_read("traversal in stat",         "/v1/stat/../secrets.env")
    escape_read("traversal in list path",    "/v1/list/../")

    # --- reads through planted symlinks ----------------------------------
    escape_read("symlink to outside file",   "/v1/content/link-to-secret")
    escape_read("symlink dir component",     "/v1/content/escape-dir/secrets.env")
    escape_read("symlink to / ",             "/v1/content/rootlink/etc/passwd")
    escape_read("nested symlink",            "/v1/content/nested/deep-link")
    escape_read("list through symlink dir",  "/v1/list/escape-dir")
    escape_read("list through / symlink",    "/v1/list/rootlink")
    escape_read("recursive list escapes",    "/v1/list/escape-dir?recursive=true")

    # --- the hard link ---------------------------------------------------
    escape_read("hard link to outside file", "/v1/content/hardlink-to-secret")

    # --- denied names ----------------------------------------------------
    escape_read("reach .git by name",        "/v1/content/.git/config")
    escape_read("reach .git via symlink dir", "/v1/content/escape-dir/.git/config")

    # --- writes that must not land outside -------------------------------
    escape_write("write through .. ",        "/v1/content/../secrets.env", secrets)
    escape_write("write through symlink",    "/v1/content/link-to-secret", secrets)
    escape_write("write via symlinked dir",  "/v1/content/escape-dir/secrets.env", secrets)
    escape_write("write to /etc/passwd",     "/v1/content/rootlink/etc/passwd",
                 Path("/etc/hostname") if Path("/etc/hostname").exists() else secrets)
    escape_write("create through ..",        "/v1/content/../planted.txt", secrets, method="POST")

    # mkdir must not create a directory outside
    st, _, _ = request("POST", "/v1/mkdir/../planted-dir")
    if st != 200 and not (REPO / "planted-dir").exists():
        R.ok("escape", "mkdir through ..", f"-> {st}")
    else:
        R.bad("escape", "mkdir through ..", f"status {st}, dir exists={(REPO/'planted-dir').exists()}")

    st, _, _ = request("POST", "/v1/mkdir/escape-dir/planted-dir")
    if st != 200 and not (REPO / "planted-dir").exists():
        R.ok("escape", "mkdir through symlink", f"-> {st}")
    else:
        R.bad("escape", "mkdir through symlink", f"status {st}")

    # move must not read from or write to outside
    for label, payload in [
        ("move src outside", {"src": "../secrets.env", "dst": "stolen.txt"}),
        ("move dst outside", {"src": "existing.txt", "dst": "../planted.txt"}),
        ("move via symlink", {"src": "escape-dir/secrets.env", "dst": "stolen.txt"}),
    ]:
        st, _, _ = request("POST", "/v1/move", body=json.dumps(payload).encode(),
                           headers={"Content-Type": "application/json"})
        st2, _, body2 = request("GET", "/v1/content/stolen.txt")
        if st != 200 and not leaked(body2) and not (REPO / "planted.txt").exists():
            R.ok("escape", label, f"-> {st}")
        else:
            R.bad("escape", label, f"status {st}, stolen={leaked(body2)}")

    # deleting a symlink must remove the LINK, never the target
    before = sha(secrets)
    st, _, _ = request("DELETE", "/v1/content/link-to-secret")
    if secrets.exists() and sha(secrets) == before:
        R.ok("escape", "DELETE symlink spares its target", f"-> {st}")
    else:
        R.bad("escape", "DELETE symlink", "the target outside the folder was affected")

    st, _, _ = request("DELETE", "/v1/content/escape-dir?recursive=true")
    still_there = (REPO / "secrets.env").exists() and (REPO / "src" / "app.py").exists()
    if still_there:
        R.ok("escape", "recursive DELETE through symlink spares the repo", f"-> {st}")
    else:
        R.bad("escape", "recursive DELETE through symlink", "DESTROYED FILES OUTSIDE THE FOLDER")


def main() -> int:
    print(f"{YELLOW}claude-files containment suite{RESET}  mode={MODE}  target={BASE}")

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
