#!/usr/bin/env python3
"""The git tools as an agent actually reaches them: through /mcp (JSON-RPC
tools/list + tools/call) and the REST routes, on the real FastAPI app.

tests/git_suite.py proves app.git_ops is right. This proves the wiring is:
which tools exist under each ENABLE_GIT_READ x ENABLE_GIT_WRITE combination,
what a read-only token may do, how a refusal reaches the caller, that a
pull is refused while an upload is open, that a tool's message tells the agent
what to do, and that a bad deployment stops the server from starting.

    python3 tests/git_surface_tests.py

Tool registration happens at import time, so each combination runs in its own
subprocess (`... child <read> <write>`); this parent just collects results.
Like tests/git_suite.py it reaches its remote over git's file transport via
test-only seams in app.git_ops; the ssh transport needs an sshd and is covered
by the container-level run.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import traceback
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

READ_TOOLS = {"git_status", "git_log", "git_diff"}
WRITE_TOOLS = {"git_commit", "git_pull", "git_push", "git_merge", "git_fetch"}
TOKEN = "rw-token-" + "a" * 24
RO_TOKEN = "ro-token-" + "b" * 24

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, bool(ok), detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{detail}]" if detail and not ok else ""))


# ----------------------------------------------------------------------------
# child: one combination, in a fresh interpreter
# ----------------------------------------------------------------------------


class Rpc:
    def __init__(self, client: Any) -> None:
        self.client = client
        self._id = 0

    def raw(self, token: str | None, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._id += 1
        headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        r = self.client.post(
            "/mcp", headers=headers,
            json={"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}},
        )
        return r.json()

    def tools(self, token: str) -> set[str]:
        return {t["name"] for t in self.raw(token, "tools/list")["result"]["tools"]}

    def call(self, token: str, name: str, args: dict[str, Any] | None = None) -> tuple[bool, Any]:
        """(ok, parsed-JSON-or-error-text)."""
        res = self.raw(token, "tools/call", {"name": name, "arguments": args or {}})
        if "error" in res:
            return False, res["error"].get("message", "")
        result = res["result"]
        text = "".join(c.get("text", "") for c in result.get("content", []))
        if result.get("isError"):
            return False, text
        try:
            return True, json.loads(text)
        except ValueError:
            return True, text


def child(read: bool, write: bool) -> int:
    from git_suite import World, seam_on, sh

    label = f"read={'on' if read else 'off'} write={'on' if write else 'off'}"
    print(f"## {label}")
    seam_on()
    w = World()
    os.environ.update(
        API_TOKEN=TOKEN, API_TOKEN_READONLY=RO_TOKEN, REQUIRE_MOUNT="false",
        DATA_ROOT=str(w.repo / "sub-folder-1"), GIT_REPO_ROOT=str(w.repo),
        ENABLE_GIT_READ="true" if read else "false", ENABLE_GIT_WRITE="true" if write else "false",
        GIT_REMOTE="origin", GIT_BRANCH="main", LOG_LEVEL="WARNING",
        RATE_LIMIT_PER_MINUTE="100000", MCP_UPLOAD_SESSION_TTL_SECONDS="900",
    )

    records: list[str] = []

    class Grab(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record.getMessage())

    for name in ("claude-files", "claude-files.mcp"):
        lg = logging.getLogger(name)
        lg.addHandler(Grab())
        lg.setLevel(logging.INFO)

    from fastapi.testclient import TestClient

    from app import main as appmain

    expected = (READ_TOOLS if read else set()) | (WRITE_TOOLS if write else set())
    auth = {"Authorization": f"Bearer {TOKEN}"}
    ro_auth = {"Authorization": f"Bearer {RO_TOKEN}"}

    with TestClient(appmain.app) as client:
        rpc = Rpc(client)

        # -- registration -----------------------------------------------------
        listed = rpc.tools(TOKEN)
        got = listed & (READ_TOOLS | WRITE_TOOLS)
        check(f"[{label}] tools/list exposes exactly the enabled git tools", got == expected,
              f"got {sorted(got)}, want {sorted(expected)}")
        check(f"[{label}] a read-only token sees the same tool list", rpc.tools(RO_TOKEN) & (READ_TOOLS | WRITE_TOOLS) == expected)
        check(f"[{label}] the file tools are untouched", {"read_file", "write_file", "begin_write"} <= listed)

        # -- folder info --------------------------------------------------------
        ok, info = rpc.call(TOKEN, "get_folder_info")
        g = info["git"]
        check(f"[{label}] get_folder_info.git reports the switches",
              ok and g["read_enabled"] is read and g["write_enabled"] is write, str(g))
        r = client.get("/v1/info", headers=auth).json()["git"]
        check(f"[{label}] /v1/info.git matches", r["read_enabled"] is read and r["write_enabled"] is write)
        if read or write:
            check(f"[{label}] info names the scope and the boundary",
                  g["scope"] == "sub-folder-1/" and g["write_boundary"] in ("kernel", "software") and g["hooks"] == "disabled", str(g))
        else:
            check(f"[{label}] info explains git is off", "disabled" in g["note"])
        if read:
            note = info["token_scope_note"]
            check(f"[{label}] the ro-token note lists git_status", "git_status" in note)

        # -- instructions ---------------------------------------------------------
        init = rpc.raw(TOKEN, "initialize", {
            "protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"},
        })["result"]["instructions"]
        if write:
            check(f"[{label}] instructions stop claiming auto-commit", "committed and pushed automatically" not in init
                  and "git_commit" in init and "yourself" in init, init[:200])
        else:
            check(f"[{label}] instructions unchanged without write tools", "committed and pushed automatically" in init)

        # -- REST: disabled classes 404, enabled ones answer ---------------------------
        read_routes = [("GET", "/v1/git/status"), ("GET", "/v1/git/log"), ("GET", "/v1/git/diff")]
        write_routes = [("POST", "/v1/git/commit", {"message": "x"}), ("POST", "/v1/git/pull", {}),
                        ("POST", "/v1/git/push", {}), ("POST", "/v1/git/merge", {"ref": "main"}),
                        ("POST", "/v1/git/fetch", {})]
        for route in read_routes:
            resp = client.get(route[1], headers=auth)
            if read:
                check(f"[{label}] REST {route[1]} answers", resp.status_code == 200, resp.text[:120])
            else:
                check(f"[{label}] REST {route[1]} is 404 git_disabled",
                      resp.status_code == 404 and resp.json()["code"] == "git_disabled", resp.text[:120])
        if not write:
            for _m, path, *body in write_routes:
                resp = client.post(path, headers=auth, json=body[0] if body else {})
                check(f"[{label}] REST {path} is 404 git_disabled",
                      resp.status_code == 404 and resp.json()["code"] == "git_disabled", resp.text[:120])
        # unauthenticated is still refused before anything git runs
        check(f"[{label}] REST git route needs a token", client.get("/v1/git/status").status_code in (401, 403))

        if read:
            # -- read-only token: reads work, fetch=true refused ------------------------
            ok, st = rpc.call(RO_TOKEN, "git_status")
            check(f"[{label}] ro token can git_status", ok and st["scope"] == "sub-folder-1/", str(st)[:150])
            ok, msg = rpc.call(RO_TOKEN, "git_status", {"fetch": True})
            want = "readonly" if write else "git_disabled"
            check(f"[{label}] git_status(fetch=true) refused ({want})", (not ok) and msg.startswith(f"Error executing tool git_status: {want}"), str(msg)[:200])
            check(f"[{label}] REST status fetch=true as ro token refused",
                  client.get("/v1/git/status?fetch=true", headers=ro_auth).status_code in (403, 404))
            w.write("sub-folder-1/a.txt", "changed\n")
            w.write("sub-folder-2/b.txt", "OUTSIDE-CONTENT\n")
            ok, d = rpc.call(TOKEN, "git_diff")
            check(f"[{label}] git_diff is scope-only through the tool", ok and "changed" in d["diff"] and "OUTSIDE-CONTENT" not in json.dumps(d))
            ok, lg = rpc.call(TOKEN, "git_log", {"limit": 3})
            check(f"[{label}] git_log works", ok and lg["commits"][0]["subject"] == "initial", str(lg)[:150])
            ok, bad = rpc.call(TOKEN, "git_diff", {"path": "../sub-folder-2/b.txt"})
            check(f"[{label}] git_diff path traversal refused", not ok)
            w.host("checkout", "--", ".")

        if write:
            # -- ro token refused for every write tool ------------------------------------
            for tool, args in (("git_commit", {"message": "x"}), ("git_push", {}), ("git_pull", {}),
                               ("git_fetch", {}), ("git_merge", {"ref": "main"})):
                ok, msg = rpc.call(RO_TOKEN, tool, args)
                check(f"[{label}] ro token refused for {tool}", (not ok) and "read-only" in msg, str(msg)[:160])
            for _m, path, *body in write_routes:
                resp = client.post(path, headers=ro_auth, json=body[0] if body else {})
                check(f"[{label}] REST {path} refused for ro token",
                      resp.status_code == 403 and resp.json()["code"] == "readonly", resp.text[:120])

            # -- the whole loop an agent runs, through the tools ----------------------------
            ok, res = rpc.call(TOKEN, "write_file", {"path": "report.md", "content": "# Report\n"})
            check(f"[{label}] write_file through MCP", ok, str(res)[:120])
            if read:
                ok, st = rpc.call(TOKEN, "git_status")
                check(f"[{label}] git_status shows the new file as in_scope",
                      ok and any(c["scope_path"] == "report.md" and c["in_scope"] for c in st["changes"]), str(st)[:200])
            ok, cm = rpc.call(TOKEN, "git_commit", {"message": "agent: add report"})
            check(f"[{label}] git_commit via MCP", ok and cm["ok"] and cm["files_changed"] == 1, str(cm)[:200])
            ok, ps = rpc.call(TOKEN, "git_push")
            origin_head = sh("rev-parse", "main", cwd=w.origin).strip()
            check(f"[{label}] git_push via MCP reaches the remote",
                  ok and ps["ok"] and origin_head == w.host("rev-parse", "HEAD").strip(), str(ps)[:200])
            resp = client.post("/v1/git/push", headers=auth, json={})
            check(f"[{label}] REST push when up to date", resp.status_code == 200 and resp.json()["pushed"] is None, resp.text[:160])

            # -- refusals carry a code, the paths, and the next step ---------------------------
            w.upstream("sub-folder-2/b.txt", "upstream edit to folder 2\n")
            ok, msg = rpc.call(TOKEN, "git_pull")
            check(f"[{label}] pull refused outside_scope, naming the path and who can fix it",
                  (not ok) and "outside_scope:" in msg and "sub-folder-2/b.txt" in msg and "host" in msg, str(msg)[:300])
            resp = client.post("/v1/git/pull", headers=auth, json={"strategy": "ff-only"})
            body = resp.json()
            check(f"[{label}] REST pull outside_scope is 409 with a detail list",
                  resp.status_code == 409 and body["code"] == "outside_scope" and "sub-folder-2/b.txt" in body["detail"]["paths"], resp.text[:200])
            check(f"[{label}] the refusal was logged as 'denied MCP git_pull'",
                  any("denied MCP git_pull -> outside_scope" in r for r in records), str(records[-3:]))
            ok, msg = rpc.call(TOKEN, "git_merge", {"ref": "--upload-pack=touch /tmp/pwned"})
            check(f"[{label}] hostile merge ref refused invalid_ref", (not ok) and "invalid_ref:" in msg, str(msg)[:200])
            resp = client.post("/v1/git/pull", headers=auth, json={"strategy": "yolo"})
            check(f"[{label}] unknown strategy is a 400", resp.status_code == 400 and resp.json()["code"] == "invalid_argument", resp.text[:160])
            check(f"[{label}] the error response is not an HTML/stack trace", resp.headers["content-type"].startswith("application/json"))

            # -- uploads in flight block a pull or merge, then release it ------------------------
            # (the guard runs before any fetch or scope check, so it wins even though
            # the remote currently holds a change outside the scope)
            ok, sess = rpc.call(TOKEN, "begin_write", {"path": "big/part.bin", "encoding": "base64"})
            check(f"[{label}] begin_write opens a session", ok, str(sess)[:100])
            ok, msg = rpc.call(TOKEN, "git_pull")
            check(f"[{label}] pull refused while a chunked upload is open, naming it",
                  (not ok) and "uploads_in_flight:" in msg and "big/part.bin" in msg, str(msg)[:300])
            ok, _ = rpc.call(TOKEN, "abort_write", {"upload_id": sess["upload_id"]})
            ok, msg = rpc.call(TOKEN, "git_pull")
            check(f"[{label}] after abort_write the guard releases (pull now reaches the scope check)",
                  (not ok) and "outside_scope:" in msg, str(msg)[:300])
            ok, tk = rpc.call(TOKEN, "request_upload", {"path": "tickets/x.bin"})
            ok2, msg = rpc.call(TOKEN, "git_merge", {"ref": "origin/main"})
            check(f"[{label}] merge refused while an upload ticket is pending, naming it",
                  ok and (not ok2) and "uploads_in_flight:" in msg and "tickets/x.bin" in msg, str(msg)[:300])

            # -- a slow git call must not freeze the server ------------------------------------------
            # Stall the remote for ~3s and fetch. If git ran on the event loop, /health (and
            # every other request) would hang for the same 3s.
            import threading
            import time as _time

            w.host("config", "remote.origin.uploadpack", "sleep 3 #")
            fetched: dict[str, Any] = {}

            def slow_fetch() -> None:
                t0 = _time.monotonic()
                fetched["res"] = rpc.call(TOKEN, "git_fetch")
                fetched["took"] = _time.monotonic() - t0

            t = threading.Thread(target=slow_fetch)
            t.start()
            _time.sleep(0.6)                      # let the fetch get into its sleep
            lat = []
            for _ in range(5):
                t0 = _time.monotonic()
                ok_health = client.get("/health").status_code == 200
                lat.append(_time.monotonic() - t0)
                assert ok_health
            t.join()
            check(f"[{label}] the slow git call really was slow", fetched["took"] >= 2.5, f"{fetched['took']:.2f}s")
            check(f"[{label}] /health stays responsive during a slow git call",
                  max(lat) < 1.0, f"worst {max(lat):.2f}s while the fetch ran")
            w.host("config", "--unset", "remote.origin.uploadpack")
            check(f"[{label}] git tools are coroutines (they run in a worker thread)", _all_async())

    return 0


def _all_async() -> bool:
    import inspect

    from app import mcp_endpoint as m

    return all(inspect.iscoroutinefunction(getattr(m, n)) for n in READ_TOOLS | WRITE_TOOLS)


# ----------------------------------------------------------------------------
# startup refusals: the server must not start in a bad git deployment
# ----------------------------------------------------------------------------


def startup_child(case: str) -> int:
    from git_suite import World, seam_on

    seam_on()
    w = World()
    env = dict(
        API_TOKEN=TOKEN, REQUIRE_MOUNT="false", DATA_ROOT=str(w.repo / "sub-folder-1"),
        GIT_REPO_ROOT=str(w.repo), ENABLE_GIT_READ="true", ENABLE_GIT_WRITE="true",
        GIT_REMOTE="origin", GIT_BRANCH="main", LOG_LEVEL="WARNING",
    )
    if case == "git_not_denied":
        env["DENIED_NAMES"] = ".gitmodules,.ssh"
    elif case == "no_git_installed":
        from app import git_ops

        git_ops.GIT_CANDIDATES = ("/nonexistent/git",)
    elif case == "root_outside_repo":
        env["DATA_ROOT"] = str(w.tmp)
    elif case == "key_without_known_hosts":
        key = w.tmp / "key"
        key.write_text("k")
        env["SSH_KEY_PATH"] = str(key)
    os.environ.update(env)
    try:
        from app import main  # noqa: F401
    except RuntimeError as exc:
        print("REFUSED:", exc)
        return 0
    print("STARTED")
    return 0


STARTUP_CASES = {
    "git_not_denied": "DENIED_NAMES",
    "no_git_installed": "WITH_GIT=1",
    "root_outside_repo": "must be GIT_REPO_ROOT",
    "key_without_known_hosts": "SSH_KNOWN_HOSTS_PATH is required",
}


# ----------------------------------------------------------------------------
# parent
# ----------------------------------------------------------------------------


def run_child(*args: str) -> tuple[int, str]:
    p = subprocess.run([sys.executable, str(Path(__file__).resolve()), "child", *args],
                       capture_output=True, text=True, timeout=300)
    return p.returncode, p.stdout + p.stderr


def main() -> int:
    failed = 0
    for read in (False, True):
        for write in (False, True):
            rc, out = run_child("combo", str(int(read)), str(int(write)))
            lines = [ln for ln in out.splitlines() if ln.startswith(("##", "  PASS", "  FAIL"))]
            print("\n".join(lines))
            if rc != 0 or any(ln.startswith("  FAIL") for ln in lines) or not any("PASS" in ln for ln in lines):
                failed += 1
                print(out[-3000:])
    print("## startup refusals")
    for case, needle in STARTUP_CASES.items():
        rc, out = run_child("startup", case)
        ok = rc == 0 and "REFUSED:" in out and needle in out and "STARTED" not in out
        print(f"  {'PASS' if ok else 'FAIL'}  server refuses to start: {case}")
        if not ok:
            failed += 1
            print(out[-1500:])
    print(f"\n{'FAILURES: ' + str(failed) if failed else 'all surface checks passed'}")
    return 1 if failed else 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "child":
        try:
            if sys.argv[2] == "combo":
                rc = child(sys.argv[3] == "1", sys.argv[4] == "1")
            else:
                rc = startup_child(sys.argv[3])
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            rc = 1
        for name, ok, detail in CHECKS:
            if not ok:
                print(f"  FAIL  {name}  [{detail}]")
        sys.exit(rc if all(ok for _, ok, _ in CHECKS) else 1)
    sys.exit(main())
