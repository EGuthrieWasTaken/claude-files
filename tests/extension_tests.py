#!/usr/bin/env python3
"""EXTRA_TOOL_MODULES (docs/extensions.md): extension tools as an agent reaches
them, through /mcp on the real FastAPI app.

What this proves: an extension's tools are listed beside the core tools and its
INSTRUCTIONS join the server instructions; a `read_only=False` tool is refused
for a read-only token before the extension's code runs, sync or async; file
access through the shared Vault keeps path containment; get_folder_info and
GET /v1/info report what loaded; and every broken extension stops the server
from starting instead of starting without it.

    python3 tests/extension_tests.py

Tool registration happens at import time, so each case runs in its own
subprocess (`... child <case>`); this parent just collects results.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import traceback
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

TOKEN = "rw-token-" + "a" * 24
RO_TOKEN = "ro-token-" + "b" * 24
READONLY_TEXT = "read-only"

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, bool(ok), detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{detail}]" if detail and not ok else ""))


# ----------------------------------------------------------------------------
# fixture extension modules, written to a temp dir on PYTHONPATH
# ----------------------------------------------------------------------------

# No `from __future__ import annotations` here, on purpose: the pinned MCP SDK
# needs real annotation objects to spot a Context parameter (see the comment
# above the imports in app/mcp_endpoint.py). docs/extensions.md says the same.
DEMO_EXT = '''
import os
from mcp.server.fastmcp import Context

INSTRUCTIONS = "DEMO-EXTENSION-INSTRUCTIONS: demo_read and demo_write exist."

_API = None


def demo_read(path: str) -> dict:
    """Read a text file through the shared Vault."""
    def _do():
        parts = _API.vault.components(path)
        fd, _st = _API.vault.open_read(parts)
        try:
            data = os.read(fd, 1 << 20)
        finally:
            os.close(fd)
        return {"path": "/".join(parts), "text": data.decode()}
    return _API.guard("demo_read", _do)


def demo_write(path: str, content: str, ctx: Context) -> dict:
    """Write a text file through the shared Vault."""
    def _do():
        parts = _API.vault.components(path)
        with _API.vault.atomic_write(parts, overwrite=True, create_parents=True) as (fd, c):
            os.write(fd, content.encode())
            c.commit(fd)
        return {"ok": True, "path": "/".join(parts), "token_rw": _API.token_is_rw(ctx)}
    return _API.guard("demo_write", _do)


async def demo_async_write(path: str, content: str) -> dict:
    """Async variant, doing its Vault work in a worker thread."""
    def _do():
        parts = _API.vault.components(path)
        with _API.vault.atomic_write(parts, overwrite=True, create_parents=True) as (fd, c):
            os.write(fd, content.encode())
            c.commit(fd)
        return {"ok": True}
    return await _API.run_in_thread("demo_async_write", _do)


def register(api):
    global _API
    _API = api
    api.add_tool(demo_read, read_only=True)
    api.add_tool(demo_write, read_only=False)
    api.add_tool(demo_async_write, read_only=False, name="demo_async_write", title="Async write")
'''

BROKEN = {
    "ext_no_register": "X = 1\n",
    "ext_future_api": "REQUIRES_API = 99\ndef register(api):\n    pass\n",
    "ext_dup_core": (
        "def read_file(path: str) -> str:\n    '''dup'''\n    return ''\n"
        "def register(api):\n    api.add_tool(read_file, read_only=True)\n"
    ),
    "ext_raises": "def register(api):\n    raise KeyError('boom')\n",
    "ext_bad_import": "import definitely_not_a_module_xyz\ndef register(api):\n    pass\n",
}

STARTUP_CASES = {
    # case: (EXTRA_TOOL_MODULES, text the refusal must contain)
    "bad_name": ("demo-ext", "not a dotted Python module name"),
    "listed_twice": ("demo_ext, demo_ext", "more than once"),
    "missing_module": ("no_such_ext_module", "failed to import"),
    "import_error_inside": ("ext_bad_import", "failed to import"),
    "no_register": ("ext_no_register", "has no register(api)"),
    "future_api": ("ext_future_api", "needs extension API 99"),
    "duplicate_core_tool": ("ext_dup_core", "already taken"),
    "register_raises": ("ext_raises", "register() raised KeyError"),
    "no_mcp_sdk": ("demo_ext", "built without the MCP SDK"),
}


def write_fixtures(base: Path) -> tuple[Path, Path]:
    mods = base / "mods"
    data = base / "data"
    mods.mkdir(parents=True)
    data.mkdir(parents=True)
    (mods / "demo_ext.py").write_text(textwrap.dedent(DEMO_EXT))
    for name, src in BROKEN.items():
        (mods / f"{name}.py").write_text(src)
    (data / "hello.txt").write_text("hello from the shared folder\n")
    (base / "outside.txt").write_text("must never be readable\n")
    return mods, data


def base_env(data: Path) -> dict[str, str]:
    return dict(
        API_TOKEN=TOKEN, API_TOKEN_READONLY=RO_TOKEN, REQUIRE_MOUNT="false",
        DATA_ROOT=str(data), LOG_LEVEL="WARNING", RATE_LIMIT_PER_MINUTE="100000",
    )


# ----------------------------------------------------------------------------
# child: one configuration, in a fresh interpreter
# ----------------------------------------------------------------------------


class Rpc:
    def __init__(self, client: Any) -> None:
        self.client = client
        self._id = 0

    def raw(self, token: str, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._id += 1
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        }
        r = self.client.post(
            "/mcp", headers=headers,
            json={"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}},
        )
        return r.json()

    def tools(self, token: str) -> dict[str, dict[str, Any]]:
        return {t["name"]: t for t in self.raw(token, "tools/list")["result"]["tools"]}

    def call(self, token: str, name: str, args: dict[str, Any] | None = None) -> tuple[bool, Any]:
        res = self.raw(token, "tools/call", {"name": name, "arguments": args or {}})
        if "error" in res:
            return False, res["error"].get("message", "")
        result = res["result"]
        text = "".join(c.get("text", "") for c in result.get("content", []))
        if result.get("isError"):
            return False, text
        try:
            return True, json.loads(text)
        except json.JSONDecodeError:
            return True, text


def child_none(data: Path) -> int:
    print("## no EXTRA_TOOL_MODULES")
    os.environ.update(base_env(data))
    os.environ.pop("EXTRA_TOOL_MODULES", None)
    from fastapi.testclient import TestClient

    from app import main as appmain

    with TestClient(appmain.app) as client:
        rpc = Rpc(client)
        names = set(rpc.tools(TOKEN))
        check("no demo tools without EXTRA_TOOL_MODULES", not any(n.startswith("demo_") for n in names))
        ok, info = rpc.call(TOKEN, "get_folder_info")
        check("get_folder_info.extensions is empty", ok and info["extensions"]["modules"] == [], str(info.get("extensions")))
        r = client.get("/v1/info", headers={"Authorization": f"Bearer {TOKEN}"}).json()
        check("/v1/info.extensions is empty", r["extensions"]["modules"] == [], str(r.get("extensions")))
    return 0


def child_demo(data: Path) -> int:
    print("## EXTRA_TOOL_MODULES=demo_ext")
    os.environ.update(base_env(data), EXTRA_TOOL_MODULES="demo_ext")
    from fastapi.testclient import TestClient

    from app import main as appmain

    with TestClient(appmain.app) as client:
        rpc = Rpc(client)

        # -- registration ------------------------------------------------------
        tools = rpc.tools(TOKEN)
        check("extension tools are listed", {"demo_read", "demo_write", "demo_async_write"} <= set(tools), str(sorted(tools)))
        check("core tools are still listed", {"read_file", "write_file", "get_folder_info"} <= set(tools))
        check("a read-only token sees the same list", set(rpc.tools(RO_TOKEN)) == set(tools))
        check("read_only=True -> readOnlyHint true", tools["demo_read"]["annotations"]["readOnlyHint"] is True)
        check("read_only=False -> readOnlyHint false", tools["demo_write"]["annotations"]["readOnlyHint"] is False)
        props = tools["demo_write"]["inputSchema"].get("properties", {})
        check("a write-gated tool keeps its real parameters (and hides ctx)",
              set(props) == {"path", "content"}, str(props))
        check("description comes from the docstring", "shared Vault" in (tools["demo_write"].get("description") or ""))
        check("title is passed through", tools["demo_async_write"].get("title") == "Async write", str(tools["demo_async_write"]))

        init = rpc.raw(TOKEN, "initialize", {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "extension-tests", "version": "0"},
        })
        instr = init.get("result", {}).get("instructions", "")
        check("INSTRUCTIONS are appended to the server instructions",
              "DEMO-EXTENSION-INSTRUCTIONS" in instr and "Call get_folder_info first" in instr, instr[-200:])

        # -- token scopes -------------------------------------------------------
        ok, res = rpc.call(TOKEN, "demo_write", {"path": "notes/a.txt", "content": "via rw"})
        check("rw token: the write tool writes", ok and (data / "notes/a.txt").read_text() == "via rw", str(res))
        check("the tool receives its Context", ok and res.get("token_rw") is True, str(res))

        ok, res = rpc.call(RO_TOKEN, "demo_write", {"path": "notes/b.txt", "content": "via ro"})
        check("ro token: a read_only=False tool is refused", not ok and READONLY_TEXT in str(res).lower(), str(res))
        check("ro token: nothing was written", not (data / "notes/b.txt").exists())

        ok, res = rpc.call(TOKEN, "demo_async_write", {"path": "notes/c.txt", "content": "async"})
        check("rw token: an async write tool works", ok and (data / "notes/c.txt").read_text() == "async", str(res))
        ok, res = rpc.call(RO_TOKEN, "demo_async_write", {"path": "notes/d.txt", "content": "async"})
        check("ro token: an async write tool is refused", not ok and READONLY_TEXT in str(res).lower(), str(res))
        check("ro token: nothing was written (async)", not (data / "notes/d.txt").exists())

        ok, res = rpc.call(RO_TOKEN, "demo_read", {"path": "hello.txt"})
        check("ro token: a read_only=True tool works", ok and "hello from" in res.get("text", ""), str(res))

        # -- containment --------------------------------------------------------
        for bad in ("../outside.txt", "/etc/passwd", "notes/../../outside.txt"):
            ok, res = rpc.call(TOKEN, "demo_read", {"path": bad})
            check(f"Vault containment holds for extension reads: {bad}",
                  not ok and "must never be readable" not in str(res), str(res))
        ok, res = rpc.call(TOKEN, "demo_write", {"path": "../escape.txt", "content": "x"})
        check("Vault containment holds for extension writes",
              not ok and not (data.parent / "escape.txt").exists(), str(res))

        # -- reporting ------------------------------------------------------------
        ok, info = rpc.call(RO_TOKEN, "get_folder_info")
        mods = info["extensions"]["modules"] if ok else None
        check("get_folder_info.extensions lists the module and its tools",
              mods == [{"module": "demo_ext", "tools": ["demo_read", "demo_write", "demo_async_write"]}], str(mods))
        check("get_folder_info.extensions reports the API version", ok and info["extensions"]["api_version"] == 1)
        r = client.get("/v1/info", headers={"Authorization": f"Bearer {RO_TOKEN}"}).json()
        check("/v1/info.extensions matches", r["extensions"] == info["extensions"], str(r.get("extensions")))
    return 0


def child_enable_mcp_off(data: Path) -> int:
    print("## ENABLE_MCP=false with EXTRA_TOOL_MODULES")
    os.environ.update(base_env(data), EXTRA_TOOL_MODULES="demo_ext", ENABLE_MCP="false")
    from app import main as appmain

    check("ENABLE_MCP=false still starts (it is the no-rebuild kill switch)", appmain._mcp_endpoint is None)
    return 0


def startup_child(case: str, data: Path) -> int:
    modules, _needle = STARTUP_CASES[case]
    os.environ.update(base_env(data), EXTRA_TOOL_MODULES=modules)
    if case == "no_mcp_sdk":
        # Stand in for a WITH_MCP=0 image: the SDK is simply not importable.
        import importlib.abc

        class _NoMcp(importlib.abc.MetaPathFinder):
            def find_spec(self, name, path=None, target=None):  # type: ignore[no-untyped-def]
                if name == "mcp" or name.startswith("mcp."):
                    raise ModuleNotFoundError(f"No module named {name!r}")
                return None

        sys.meta_path.insert(0, _NoMcp())
    try:
        from app import main  # noqa: F401
    except RuntimeError as exc:
        print("REFUSED:", exc)
        return 0
    print("STARTED")
    return 0


# ----------------------------------------------------------------------------
# parent
# ----------------------------------------------------------------------------


def run_child(mods: Path, data: Path, *args: str) -> tuple[int, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(mods), str(ROOT)])
    for k in ("EXTRA_TOOL_MODULES", "ENABLE_MCP", "API_TOKEN", "API_TOKEN_READONLY"):
        env.pop(k, None)
    p = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "child", str(data), *args],
        capture_output=True, text=True, timeout=300, env=env,
    )
    return p.returncode, p.stdout + p.stderr


def main() -> int:
    failed = 0
    with tempfile.TemporaryDirectory(prefix="cf-ext-") as tmp:
        for case in ("none", "demo", "enable_mcp_off"):
            mods, data = write_fixtures(Path(tmp) / case)
            rc, out = run_child(mods, data, case)
            lines = [ln for ln in out.splitlines() if ln.startswith(("##", "  PASS", "  FAIL"))]
            print("\n".join(lines))
            if rc != 0 or any(ln.startswith("  FAIL") for ln in lines) or not any("PASS" in ln for ln in lines):
                failed += 1
                print(out[-3000:])
        print("## startup refusals")
        for case, (_mods, needle) in STARTUP_CASES.items():
            mods, data = write_fixtures(Path(tmp) / f"startup-{case}")
            rc, out = run_child(mods, data, "startup", case)
            ok = rc == 0 and "REFUSED:" in out and needle in out and "STARTED" not in out
            print(f"  {'PASS' if ok else 'FAIL'}  server refuses to start: {case}")
            if not ok:
                failed += 1
                print(out[-1500:])
    print(f"\n{'FAILURES: ' + str(failed) if failed else 'all extension checks passed'}")
    return 1 if failed else 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "child":
        data = Path(sys.argv[2])
        try:
            if sys.argv[3] == "startup":
                rc = startup_child(sys.argv[4], data)
            else:
                rc = {"none": child_none, "demo": child_demo, "enable_mcp_off": child_enable_mcp_off}[sys.argv[3]](data)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            rc = 1
        for name, ok, detail in CHECKS:
            if not ok:
                print(f"  FAIL  {name}  [{detail}]")
        sys.exit(rc if all(ok for _, ok, _ in CHECKS) else 1)
    sys.exit(main())
