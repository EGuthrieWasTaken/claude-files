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
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

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
            api_key: str | None = None,
            headers: dict | None = None) -> tuple[int, dict, bytes]:
    """Send a request with the path EXACTLY as given -- no normalisation."""
    host, _, port = BASE.partition(":")
    conn = http.client.HTTPConnection(host, int(port or 80), timeout=30)
    hdrs = dict(headers or {})
    if token:
        hdrs["Authorization"] = f"Bearer {token}"
    if api_key:
        hdrs["X-API-Key"] = api_key
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

    st, _, body = request("PUT", "/v1/content/miss/ing/deep.txt", body=b"x")
    payload_ok = st == 409 and json.loads(body)["code"] == "no_parent" \
        and "miss" in json.loads(body)["message"]
    if payload_ok:
        R.ok("func", "write to a missing parent -> 409 no_parent naming the folder")
    else:
        R.bad("func", "no_parent", f"status {st} body {body[:200]!r}")

    st, _, _ = request("PUT", "/v1/content/miss/ing/deep.txt?parents=true", body=b"deep")
    st2, _, body2 = request("GET", "/v1/content/miss/ing/deep.txt")
    if st == 200 and body2 == b"deep":
        R.ok("func", "?parents=true creates the whole chain")
    else:
        R.bad("func", "parents=true", f"got {st}/{st2}")

    st, _, _ = request("POST", "/v1/content/miss2/a/b.txt?parents=true", body=b"p")
    R.ok("func", "POST honours ?parents=true") if st == 200 \
        else R.bad("func", "POST parents", f"status {st}")

    st, _, _ = request("POST", "/v1/move", body=json.dumps(
        {"src": "miss/ing/deep.txt", "dst": "moved/into/here.txt", "parents": True}).encode(),
        headers={"Content-Type": "application/json"})
    st2, _, _ = request("GET", "/v1/content/moved/into/here.txt")
    if st == 200 and st2 == 200:
        R.ok("func", "move honours parents:true")
    else:
        R.bad("func", "move parents", f"got {st}/{st2}")

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

        _st, _, raw = request("PUT", "/v1/content/ro.txt", body=b"x", token=RO_TOKEN)
        msg = json.loads(raw).get("message", "")
        if "read-only" in msg and "read-write token" in msg:
            R.ok("func", "read-only refusal tells the caller what to do next")
        else:
            R.bad("func", "read-only refusal message", f"{msg!r}"[:200])

        infos = {}
        for label, tok in (("rw", TOKEN), ("ro", RO_TOKEN)):
            st, _, raw = request("GET", "/v1/info", token=tok)
            infos[label] = json.loads(raw) if st == 200 else {}
        if infos["rw"].get("token_scope") == "rw" and infos["rw"].get("writes_allowed") is True \
                and infos["ro"].get("token_scope") == "ro" and infos["ro"].get("writes_allowed") is False:
            R.ok("func", "/v1/info reports token_scope and writes_allowed per token")
        else:
            R.bad("func", "/v1/info token scope", f"{infos!r}"[:200])

    # --- X-API-Key: the header the MCP connector dialog sends -------------
    # (docs/plan-mcp-connector.md, test 9)
    st, _, _ = request("GET", "/v1/list", token=None, api_key=TOKEN)
    R.ok("func", "X-API-Key accepted where Authorization is today") if st == 200 \
        else R.bad("func", "X-API-Key accepted", f"status {st}")

    st, _, _ = request("GET", "/v1/list", token=None, api_key="wrong-token-0123456789abcdef")
    R.ok("func", "wrong X-API-Key -> 401") if st == 401 \
        else R.bad("func", "wrong X-API-Key", f"status {st}")

    if RO_TOKEN:
        st, _, _ = request("GET", "/v1/list", token=None, api_key=RO_TOKEN)
        st2, _, _ = request("PUT", "/v1/content/ro2.txt", body=b"x", token=None, api_key=RO_TOKEN)
        if st == 200 and st2 == 403:
            R.ok("func", "read-only token via X-API-Key stays read-only")
        else:
            R.bad("func", "read-only X-API-Key", f"got {st}/{st2}, wanted 200/403")

    # Authorization wins when both headers are present with different values
    # -- never "try each until one matches" (that would be two guesses
    # against the rate limiter from one request).
    st, _, _ = request("GET", "/v1/list", token=TOKEN, api_key="wrong-token-0123456789abcdef")
    R.ok("func", "Authorization wins over a conflicting X-API-Key") if st == 200 \
        else R.bad("func", "Authorization precedence", f"status {st}, wanted 200")


# ==========================================================================
# UPLOAD TICKETS -- one-time upload URLs (docs/upload-urls.md)
# ==========================================================================

# Every ticket secret this run hands out, so main() can print them for
# run_tests.sh to confirm none of them ever reach a container log line.
_SEEN_SECRETS: list[str] = []


def _create_ticket(body: dict, *, token: str | None = TOKEN) -> tuple[int, dict, bytes]:
    st, headers, resp_body = request(
        "POST", "/v1/uploads", body=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, token=token,
    )
    if st == 201:
        secret = urlsplit(json.loads(resp_body)["upload_url"]).path.rsplit("/", 1)[-1]
        _SEEN_SECRETS.append(secret)
    return st, headers, resp_body


def upload_tickets() -> None:
    print(f"\n{YELLOW}UPLOAD TICKETS -- one-time upload URLs{RESET}")

    # -- the whole flow works end to end -------------------------------
    payload = os.urandom(50_000)
    want_sha = hashlib.sha256(payload).hexdigest()
    st, _, body = _create_ticket({"path": "uploads/ticket-test.bin"})
    ticket = json.loads(body) if st == 201 else {}
    if st != 201 or "upload_url" not in ticket:
        R.bad("upload", "create ticket", f"status {st} body {body[:200]!r}")
        return  # nothing below can run without a working ticket

    R.ok("upload", "create ticket")
    put_path = urlsplit(ticket["upload_url"]).path

    st, _, body = request("PUT", put_path, body=payload, token=None)
    result = json.loads(body) if st == 200 else {}
    if st == 200 and result.get("sha256") == want_sha and result.get("size") == len(payload):
        R.ok("upload", "PUT with no bearer token uploads the bytes")
    else:
        R.bad("upload", "PUT bytes", f"status {st} body {body[:200]!r}")

    st, _, body = request("GET", f"/v1/uploads/{ticket['upload_id']}")
    status = json.loads(body) if st == 200 else {}
    if st == 200 and status.get("state") == "complete" and status.get("sha256") == want_sha:
        R.ok("upload", "status shows complete with matching sha256")
    else:
        R.bad("upload", "status", f"status {st} body {body[:200]!r}")

    st, _, body = request("GET", "/v1/content/uploads/ticket-test.bin")
    if st == 200 and body == payload:
        R.ok("upload", "uploaded bytes round-trip through the normal read path")
    else:
        R.bad("upload", "round-trip", f"status {st}, {len(body)} bytes")

    # -- single use -----------------------------------------------------
    st, _, _ = request("PUT", put_path, body=b"again", token=None)
    R.ok("upload", "reusing a completed ticket -> 409") if st == 409 \
        else R.bad("upload", "reuse", f"status {st}, wanted 409")

    # -- a bad secret and a bad id are indistinguishable -----------------
    bad_secret_path = put_path.rsplit("/", 1)[0] + "/wrong-secret-0123456789abcdef0123456789"
    st, _, _ = request("PUT", bad_secret_path, body=b"x", token=None)
    R.ok("upload", "wrong secret -> 404") if st == 404 \
        else R.bad("upload", "wrong secret", f"status {st}, wanted 404")

    st, _, _ = request("PUT", "/v1/uploads/u_doesnotexist/anysecret0123456789abcdef",
                        body=b"x", token=None)
    R.ok("upload", "unknown upload_id -> 404") if st == 404 \
        else R.bad("upload", "unknown id", f"status {st}, wanted 404")

    # -- read-only tokens can't create tickets ---------------------------
    if RO_TOKEN:
        st, _, _ = _create_ticket({"path": "uploads/ro-should-fail.bin"}, token=RO_TOKEN)
        R.ok("upload", "read-only token cannot create a ticket -> 403") if st == 403 \
            else R.bad("upload", "ro create ticket", f"status {st}, wanted 403")

    # -- overwrite=false onto an existing file -> 409 at creation --------
    st, _, _ = _create_ticket({"path": "uploads/ticket-test.bin", "overwrite": False})
    R.ok("upload", "overwrite=false onto existing file -> 409 at creation") if st == 409 \
        else R.bad("upload", "overwrite=false", f"status {st}, wanted 409")

    # -- expect_sha256 mismatch: nothing lands, no tmp file, ticket failed
    st, _, body = _create_ticket({"path": "uploads/badhash.bin", "expect_sha256": "0" * 64})
    if st == 201:
        ticket2 = json.loads(body)
        put_path2 = urlsplit(ticket2["upload_url"]).path
        st2, _, _ = request("PUT", put_path2, body=b"not the right bytes", token=None)
        st3, _, _ = request("GET", "/v1/content/uploads/badhash.bin")
        st4, _, body4 = request("GET", f"/v1/uploads/{ticket2['upload_id']}")
        status4 = json.loads(body4) if st4 == 200 else {}
        no_tmp_left = not any(
            p.name.startswith(".upload-") and p.name.endswith(".tmp")
            for p in (REPO / "shared" / "uploads").glob("*")
        ) if (REPO / "shared" / "uploads").is_dir() else True
        if (st2 not in (200,) and st3 == 404 and status4.get("state") == "failed"
                and status4.get("error", {}).get("code") == "sha_mismatch" and no_tmp_left):
            R.ok("upload", "expect_sha256 mismatch leaves nothing behind")
        else:
            R.bad("upload", "sha mismatch",
                  f"put={st2} content={st3} ticket={status4} tmp_left={not no_tmp_left}")
    else:
        R.bad("upload", "sha mismatch ticket create", f"status {st}")

    # -- scope: a ticket outside the folder is refused at creation -------
    for label, path in [
        ("traversal", "../outside.txt"),
        ("absolute", "/etc/x"),
        ("denied name", ".git/config"),
    ]:
        st, _, body = _create_ticket({"path": path})
        R.ok("upload", f"ticket for {label} path refused at creation -> {st}") if st in (400, 403) \
            else R.bad("upload", f"ticket scope ({label})", f"status {st} body {body[:200]!r}")

    st, _, body = _create_ticket({"path": "escape-dir/planted.txt"})
    R.ok("upload", "ticket through a symlinked parent refused at creation") if st in (400, 403) \
        else R.bad("upload", "ticket via symlinked parent", f"status {st} body {body[:200]!r}")

    # -- a symlink planted AFTER the ticket is issued is still refused ---
    secrets_outside = REPO / "secrets.env"
    before = sha(secrets_outside)
    st, _, body = _create_ticket({"path": "evil-link"})
    if st == 201:
        ticket3 = json.loads(body)
        link_path = REPO / "shared" / "evil-link"
        try:
            os.symlink("../secrets.env", link_path)
            put_path3 = urlsplit(ticket3["upload_url"]).path
            st2, _, body2 = request("PUT", put_path3, body=b"PWNED_BY_TEST", token=None)
            after = sha(secrets_outside)
            hit = leaked(body2)
            if st2 != 200 and after == before and not hit:
                R.ok("upload", "symlink planted after ticket issued still refused")
            else:
                R.bad("upload", "post-issue symlink",
                      f"status {st2}, unchanged={after == before}, leaked={hit}")
        finally:
            try:
                link_path.unlink()
            except OSError:
                pass
    else:
        R.bad("upload", "post-issue symlink ticket create", f"status {st}")

    # -- size cap enforced on the ticket route too -----------------------
    st, _, body = _create_ticket({"path": "uploads/toobig.bin"})
    if st == 201:
        ticket4 = json.loads(body)
        put_path4 = urlsplit(ticket4["upload_url"]).path
        big = b"x" * (ticket4["max_bytes"] + 1)
        st2, _, _ = request("PUT", put_path4, body=big, token=None)
        R.ok("upload", "oversize ticket upload -> 413") if st2 == 413 \
            else R.bad("upload", "ticket size limit", f"status {st2}, wanted 413")
    else:
        R.bad("upload", "size cap ticket create", f"status {st}")

    # -- expiry: a ticket left unused expires ----------------------------
    ttl = float(os.environ.get("UPLOAD_TICKET_TTL_SECONDS", "900"))
    if ttl <= 5:
        st, _, body = _create_ticket({"path": "uploads/never-used.bin"})
        if st == 201:
            ticket5 = json.loads(body)
            time.sleep(ttl + 0.5)
            put_path5 = urlsplit(ticket5["upload_url"]).path
            st2, _, _ = request("PUT", put_path5, body=b"too late", token=None)
            R.ok("upload", f"unused ticket expires after {ttl:.0f}s -> 410") if st2 == 410 \
                else R.bad("upload", "expiry", f"status {st2}, wanted 410")
        else:
            R.bad("upload", "expiry ticket create", f"status {st}")
    else:
        print(f"  {DIM}skip{RESET} [upload] expiry check "
              f"(UPLOAD_TICKET_TTL_SECONDS={ttl:.0f}s, too slow to wait out here)")


# ==========================================================================
# DOWNLOAD TICKETS -- one-time download URLs (docs/download-urls.md)
# ==========================================================================

def _create_download(path: str, *, token: str | None = TOKEN) -> tuple[int, dict, bytes]:
    st, headers, resp_body = request(
        "POST", "/v1/downloads", body=json.dumps({"path": path}).encode(),
        headers={"Content-Type": "application/json"}, token=token,
    )
    if st == 201:
        secret = urlsplit(json.loads(resp_body)["download_url"]).path.rsplit("/", 1)[-1]
        _SEEN_SECRETS.append(secret)
    return st, headers, resp_body


def download_tickets() -> None:
    print(f"\n{YELLOW}DOWNLOAD TICKETS -- one-time download URLs{RESET}")

    payload = b"%PDF-1.7\x00" + os.urandom(50_000)
    want_sha = hashlib.sha256(payload).hexdigest()
    (REPO / "shared" / "dl-ticket-test.pdf").write_bytes(payload)

    st, _, body = _create_download("dl-ticket-test.pdf")
    ticket = json.loads(body) if st == 201 else {}
    if st != 201 or "download_url" not in ticket:
        R.bad("download", "create ticket", f"status {st} body {body[:200]!r}")
        return
    if ticket.get("sha256") == want_sha and ticket.get("size") == len(payload):
        R.ok("download", "create ticket returns size and sha256")
    else:
        R.bad("download", "ticket size/sha256", f"{ticket!r}"[:200])
    get_path = urlsplit(ticket["download_url"]).path

    st, hdrs, body = request("GET", get_path, token=None)
    if st == 200 and body == payload and "attachment" in {k.lower(): v for k, v in hdrs.items()}.get("content-disposition", ""):
        R.ok("download", "GET with no bearer token returns the exact bytes as an attachment")
    else:
        R.bad("download", "GET bytes", f"status {st} len {len(body)}")

    st, _, body = request("GET", f"/v1/downloads/{ticket['download_id']}")
    status = json.loads(body) if st == 200 else {}
    if status.get("state") == "complete" and status.get("bytes_sent") == len(payload):
        R.ok("download", "status endpoint reports complete")
    else:
        R.bad("download", "status", f"status {st} body {body[:200]!r}")

    st, _, _ = request("GET", get_path, token=None)
    R.ok("download", "reusing a completed ticket -> 409") if st == 409 \
        else R.bad("download", "reuse", f"status {st}, wanted 409")

    st, _, _ = request("GET", get_path.rsplit("/", 1)[0] + "/not-the-secret", token=None)
    R.ok("download", "wrong secret -> 404") if st == 404 \
        else R.bad("download", "wrong secret", f"status {st}, wanted 404")

    st, _, _ = request("GET", "/v1/downloads/" + ticket["download_id"], token=None)
    R.ok("download", "status endpoint needs a bearer token") if st == 401 \
        else R.bad("download", "status without token", f"status {st}, wanted 401")

    if RO_TOKEN:
        st, _, body = _create_download("dl-ticket-test.pdf", token=RO_TOKEN)
        if st == 201:
            ro_path = urlsplit(json.loads(body)["download_url"]).path
            st2, _, body2 = request("GET", ro_path, token=None)
            R.ok("download", "read-only token can create a ticket and download") \
                if st2 == 200 and body2 == payload \
                else R.bad("download", "ro download", f"status {st2}")
        else:
            R.bad("download", "ro create ticket", f"status {st}, wanted 201")

    st, _, _ = _create_download("no-such-file.pdf")
    R.ok("download", "ticket for a missing file refused at creation -> 404") if st == 404 \
        else R.bad("download", "missing file", f"status {st}, wanted 404")

    for label, path in [
        ("traversal", "../secrets.env"),
        ("absolute", "/etc/passwd"),
        (".git component", ".git/config"),
        ("symlink to outside", "link-to-secret"),
        ("symlinked parent", "escape-dir/secrets.env"),
        ("hard link to outside", "hardlink-to-secret"),
    ]:
        st, _, body = _create_download(path)
        R.ok("download", f"ticket for {label} path refused at creation -> {st}") \
            if st in (400, 403, 404) and not leaked(body) \
            else R.bad("download", f"ticket scope ({label})", f"status {st} body {body[:200]!r}")

    swap = REPO / "shared" / "dl-swap.txt"
    swap.write_bytes(b"harmless")
    st, _, body = _create_download("dl-swap.txt")
    if st == 201:
        swap.unlink()
        swap.symlink_to("../secrets.env")
        st2, _, body2 = request("GET", urlsplit(json.loads(body)["download_url"]).path, token=None)
        R.ok("download", "symlink planted after ticket issued is not followed") \
            if st2 != 200 and not leaked(body2) \
            else R.bad("download", "post-issue symlink", f"status {st2} body {body2[:100]!r}")
        swap.unlink()
    else:
        R.bad("download", "swap ticket create", f"status {st}")

    ttl = float(os.environ.get("DOWNLOAD_TICKET_TTL_SECONDS", "900"))
    if ttl <= 5:
        st, _, body = _create_download("dl-ticket-test.pdf")
        if st == 201:
            time.sleep(ttl + 0.5)
            st2, _, _ = request("GET", urlsplit(json.loads(body)["download_url"]).path, token=None)
            R.ok("download", f"unused ticket expires after {ttl:.0f}s -> 410") if st2 == 410 \
                else R.bad("download", "expiry", f"status {st2}, wanted 410")
        else:
            R.bad("download", "expiry ticket create", f"status {st}")
    else:
        print(f"  {DIM}skip{RESET} [download] expiry check "
              f"(DOWNLOAD_TICKET_TTL_SECONDS={ttl:.0f}s, too slow to wait out here)")


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

    # ?parents=true creates directories, so re-run the write attacks with it on
    escape_write("parents=true through ..",      "/v1/content/../pwned.txt?parents=true", secrets)
    escape_write("parents=true via symlink dir", "/v1/content/escape-dir/pwned.txt?parents=true", secrets)
    escape_write("parents=true deep traversal",  "/v1/content/../../etc/pwned?parents=true", secrets)
    escape_read("parents=true into .git",        "/v1/content/.git/hooks/evil?parents=true")

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

    # deleting a symlink must remove the LINK, never the target. Uses its own
    # dedicated symlinks (not link-to-secret/escape-dir) so this doesn't
    # consume the read/write escape targets mcp_escape_suite.py runs against
    # the same fixture right after this suite.
    before = sha(secrets)
    st, _, _ = request("DELETE", "/v1/content/link-to-secret-del")
    if secrets.exists() and sha(secrets) == before:
        R.ok("escape", "DELETE symlink spares its target", f"-> {st}")
    else:
        R.bad("escape", "DELETE symlink", "the target outside the folder was affected")

    st, _, _ = request("DELETE", "/v1/content/escape-dir-del?recursive=true")
    still_there = (REPO / "secrets.env").exists() and (REPO / "src" / "app.py").exists()
    if still_there:
        R.ok("escape", "recursive DELETE through symlink spares the repo", f"-> {st}")
    else:
        R.bad("escape", "recursive DELETE through symlink", "DESTROYED FILES OUTSIDE THE FOLDER")


def main() -> int:
    print(f"{YELLOW}claude-files containment suite{RESET}  mode={MODE}  target={BASE}")

    before = snapshot()
    functional()
    upload_tickets()
    download_tickets()
    escapes()
    after = snapshot()

    print(f"\n{YELLOW}INTEGRITY -- nothing outside the shared folder changed{RESET}")
    if before == after:
        R.ok("integrity", f"{len(before)} paths outside shared/ are byte-identical")
    else:
        changed = {k for k in set(before) | set(after) if before.get(k) != after.get(k)}
        R.bad("integrity", "files outside shared/ changed", f"{sorted(changed)}")

    # Printed on their own marked lines (not logged anywhere else) so
    # run_tests.sh can grep the container's own logs and confirm none of
    # these one-time secrets ever appear in them.
    for secret in _SEEN_SECRETS:
        print(f"UPLOAD_SECRET_USED:{secret}")

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
