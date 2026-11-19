"""Streamable HTTP MCP endpoint mounted at /mcp.

Design brief: docs/plan-mcp-connector.md -- read that before touching this
file. It mirrors meeting-transcriber/app/mcp_endpoint.py closely enough that
a reader of one recognises the other; four gotchas were found there and
reproduce here unchanged:

1. **Mounting is not enough.** `mcp.streamable_http_app()` returns a
   Starlette app whose own `lifespan=` starts its session manager --
   Starlette does not forward ASGI lifespan events into a *mounted*
   sub-application, so that lifespan never runs and the first tool call
   fails with "Task group is not initialized. Make sure to use run()."
   `app/main.py`'s own `lifespan()` enters `mcp.session_manager.run()`
   explicitly via `_mcp_session_scope()`, once this module is mounted.
   `mcp.session_manager` can't be touched until `streamable_http_app()` has
   been called once (it's built lazily), so `mount_mcp()` must run before
   `app/main.py`'s lifespan does -- which it does by construction (mounting
   happens at import time; lifespan runs at startup).

2. **`streamable_http_path` must be "/", not the SDK's default `/mcp`.**
   `streamable_http_app()` registers its own route at that setting *inside*
   the sub-app; mounting that sub-app at "/mcp" while leaving the default
   would put the real endpoint at "/mcp/mcp".

3. **A bare "/mcp" (no trailing slash) can never just 307 to "/mcp/".**
   `app.mount("/mcp", sub_app)` is a Starlette `Mount`, which always compiles
   its path as `"/mcp" + "/{path:path}"` -- there is no configuration that
   makes a `Mount` match the exact prefix with nothing after it, so a
   request for literal "/mcp" 404s out of route matching and
   `redirect_slashes` turns that into a 307 to "/mcp/". claude.ai's
   custom-connector "Add" flow probes the configured URL with its static
   auth header already attached and refuses to follow any redirect while
   doing so, surfacing as `static_headers_probe_redirect`.
   `_ExactPathForward` below is a second, non-`Mount` route registered for
   the literal "/mcp" that rewrites `scope["path"]` to "/" and calls the
   sub-app directly, so both "/mcp" and "/mcp/" resolve without a redirect.
   It must be a callable *object*, not a plain function -- Starlette's
   `Route` detects `inspect.isfunction`/`ismethod` endpoints and wraps them
   with `request_response()`, which expects `func(request) -> Response` and
   would reject a raw ASGI callable outright.

4. **The SDK's DNS-rebinding guard 421s real deployments.**
   `TransportSecuritySettings(enable_dns_rebinding_protection=True)` is the
   SDK default and only allows `Host: localhost` variants, so every request
   through the Cloudflare tunnel hostname is rejected before reaching a
   tool. `MCP_PUBLIC_HOSTNAME` allow-lists the real hostname; left unset,
   the guard is disabled outright (logged once) rather than 421ing silently
   -- `/mcp` is gated by the token check either way.

Auth: reuse `_check_token()` (`app/main.py`) via middleware wrapping the
mounted sub-app, exactly as meeting-transcriber's `_RequireAuth` does --
FastAPI's `Depends` machinery (`ReadAuth`/`WriteAuth`) does not apply to a
raw mounted Starlette app.

`app.main` and this module import each other (main mounts this module; this
module calls back into main's `CONFIG`/`VAULT`/`_check_token`/`_rate_limit`),
so every reference to `app.main` here is `from . import main as appmain`
*inside* a function body, looked up at call time rather than at import time.
The exceptions are `_transport_security()` and `_git_mode()`, called once at
module load to build the `mcp` object and the tool descriptions below -- they
do the same deferred lookup, but their calls happen synchronously during
`app/main.py`'s own bottom-of-file `from . import mcp_endpoint` statement, at
which point `app.main.CONFIG` is already set (it's assigned near the top of
that file, long before the import that pulls this module in).
"""

# Deliberately NOT `from __future__ import annotations`, unlike every other
# module in this repo. mcp==1.12.4's Tool.from_function() (tools/base.py)
# reads `inspect.signature(fn)` without `eval_str=True` and then calls
# `issubclass(param.annotation, Context)` on whatever it gets -- with PEP 563
# active, every annotation in this module would arrive as a plain string
# (e.g. "str", "int | None"), and `issubclass("str", Context)` raises
# `TypeError: issubclass() arg 1 must be a class` for literally any
# parameter, not just Context/union ones. Reproduced directly against the
# pinned SDK: every `@mcp.tool()` below fails at import time with the future
# import present. This module targets Python 3.12 (the Dockerfile's base
# image) so the modern `X | None` / `list[...]` syntax below works natively
# without it.
import base64
import asyncio
import binascii
import contextlib
import hashlib
import hmac
import io
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import FastAPI, HTTPException
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from . import extensions as _ext
from .git_ops import GitOpError, GitRepo
from .safepath import PathError

log = logging.getLogger("claude-files.mcp")

_PREVIEW_CHARS = 2000

_INSTRUCTIONS = """\
A file API scoped to one shared folder on a remote machine, exposed here as
MCP tools instead of raw REST calls.

{persistence}

To upload a file that already exists on your machine, use request_upload
and run the curl command it returns -- never paste a file's contents into
write_file or write_chunk. write_file is for short text you are composing
now.

To read a file that is binary (a PDF, an image) or too big for read_file's
inline budget, use request_download and run the curl command it returns.

Call get_folder_info first -- it reports this token's scope (rw/ro), the
server's limits, and the inline size budgets read_file/write_file operate
under. A file too large or too binary for a tool result is refused with an
explanation naming the equivalent REST endpoint, never silently truncated
or mangled.
"""

# What an agent is told becomes of a write once it lands. Exactly one applies,
# picked once at startup by _git_mode(). AGENT.md's "If this folder is a git
# repository" section says the same things to REST agents; keep them in step.
#
#   autocommit  AUTOCOMMIT_NOTICE=true: the operator runs the autocommit
#               sidecar (autocommit/), which commits and pushes for the agent.
#   git_tools   ENABLE_GIT_WRITE=true: the agent commits and pushes itself.
#               Wins over AUTOCOMMIT_NOTICE, whose claim would then be false.
#   none        The default. No claim about git at all: the server can't see
#               whether anything commits its folder, so it says nothing.
_PERSISTENCE = {
    "autocommit": (
        "This folder is a git repository. Anything written through these tools is\n"
        "committed and pushed automatically, usually within a couple of minutes --\n"
        "writes are effectively permanent and enter public git history.\n"
    ),
    "git_tools": (
        "This folder is inside a git repository, and nothing is committed or pushed\n"
        "automatically: when you want your changes in git, call git_status, then\n"
        "git_commit and git_push yourself. Once pushed, writes are effectively\n"
        "permanent and enter public git history.\n"
    ),
    "none": (
        "Files written here persist on the host, where anyone else with access to\n"
        "this folder -- people or other agents -- can read them.\n"
    ),
}

# Follows the paragraph above in every mode.
_HANDLING = (
    "Never write credentials, tokens, keys, or personal data. There is no append\n"
    "or partial edit, only whole-file replace: assemble a file completely, then\n"
    "write it once, rather than using the folder as scratch space."
)

# The last sentence of each tool description that depends on the mode, filled
# in by _git_note(). A tool description is read at the moment of use and the
# instructions may not be (docs/plan-mcp-connector.md, "Self-documenting the
# connector"), so each repeats the part of _PERSISTENCE that matters for it.
_TOOL_GIT_NOTES = {
    "write_file": {
        "autocommit": (
            "Remember: this folder is a git repository and writes are committed "
            "and pushed automatically -- never write credentials or secrets."
        ),
        "git_tools": (
            "Nothing is committed until you call git_commit, but once pushed a "
            "write is permanent git history -- never write credentials or secrets."
        ),
        "none": (
            "Others with access to this folder can read what you write -- never "
            "write credentials or secrets."
        ),
    },
    "delete_file": {
        "autocommit": (
            "Deleting does NOT remove the file from git history: the shared "
            "folder is auto-committed and pushed, so a prior version remains "
            "reachable in `git log` even after this call."
        ),
        "git_tools": (
            "Deleting does NOT remove the file from git history: a version "
            "already committed stays reachable in `git log`, and the deletion "
            "itself reaches git only when you git_commit it."
        ),
        "none": "",
    },
}


def _transport_security() -> TransportSecuritySettings:
    from . import main as appmain

    if appmain.CONFIG.mcp_public_hostname:
        host = appmain.CONFIG.mcp_public_hostname
        return TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[host],
            allowed_origins=[f"https://{host}", f"http://{host}"],
        )
    log.warning(
        "MCP_PUBLIC_HOSTNAME is not set -- disabling the MCP SDK's DNS-rebinding "
        "Host-header check for /mcp (its default only allows Host: localhost, which "
        "would reject every real request through the tunnel with 421). /mcp is still "
        "gated by the same token check as every other route; set MCP_PUBLIC_HOSTNAME "
        "to this service's public hostname to restore the SDK's own check as well."
    )
    return TransportSecuritySettings(enable_dns_rebinding_protection=False)


def _git_mode() -> str:
    """Which _PERSISTENCE paragraph this deployment gets ("autocommit",
    "git_tools" or "none"). Read once, at import, like _transport_security():
    the instructions and tool descriptions are fixed when the tools register."""
    from . import main as appmain

    cfg = appmain.CONFIG
    if cfg.enable_git_write:
        if cfg.autocommit_notice:
            log.warning(
                "AUTOCOMMIT_NOTICE is ignored because ENABLE_GIT_WRITE is on: agents are "
                "told to commit and push with the git tools themselves. Don't also run the "
                "autocommit sidecar on this repository; the two race on git's index lock "
                "(README.md, \"Working alongside everything else\")."
            )
        return "git_tools"
    return "autocommit" if cfg.autocommit_notice else "none"


_GIT_MODE = _git_mode()


def _build_instructions() -> str:
    return _INSTRUCTIONS.replace("{persistence}", _PERSISTENCE[_GIT_MODE] + _HANDLING)


def _git_note(fn):  # type: ignore[no-untyped-def]
    """Replace the `{git_note}` marker in a tool's docstring with that tool's
    sentence for _GIT_MODE. Apply it UNDER @mcp.tool(): decorators run bottom
    up, so the SDK reads the docstring as its description after this edit."""
    assert fn.__doc__ and "{git_note}" in fn.__doc__, fn.__name__
    fn.__doc__ = fn.__doc__.replace("{git_note}", _TOOL_GIT_NOTES[fn.__name__][_GIT_MODE]).rstrip()
    return fn


def _load_extensions() -> list[_ext.LoadedExtension]:
    """Import the EXTRA_TOOL_MODULES (docs/extensions.md) before the server is
    built, so their INSTRUCTIONS can join the server instructions. Their tools
    are registered at the bottom of this module, after the core ones."""
    from . import main as appmain

    return _ext.load(appmain.CONFIG.extra_tool_modules)


_EXTENSIONS = _load_extensions()

mcp = FastMCP(
    "claude-files",
    instructions=_ext.combined_instructions(_build_instructions(), _EXTENSIONS),
    streamable_http_path="/",
    transport_security=_transport_security(),
    stateless_http=True,
    json_response=True,
)


class _RequireAuth(BaseHTTPMiddleware):
    """Gate the whole /mcp surface behind the same check as every REST route.

    An unauthenticated request never reaches a tool body -- no tool should
    ever be the first thing to check a credential. Also applies the shared
    rate limiter: one deque across REST and MCP, so a connector session
    doesn't get an unlimited budget just because it skipped the FastAPI
    dependency graph.
    """

    async def dispatch(self, request, call_next):  # type: ignore[no-untyped-def]
        from . import main as appmain

        try:
            appmain._rate_limit()
            scope = appmain._check_token(
                request.headers.get("authorization"),
                request.headers.get("x-api-key"),
            )
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, dict) else {
                "code": "error", "message": str(exc.detail)
            }
            return JSONResponse(detail, status_code=exc.status_code, headers=exc.headers or {})
        request.state.token_scope = scope
        return await call_next(request)


class _ExactPathForward:
    """Dispatch straight into `app` with scope["path"] rewritten to "/".

    See this module's docstring, gotcha 3: a Starlette `Mount` can only ever
    match "/mcp/...", never the bare "/mcp". Must be a callable object, not
    a plain function -- see gotcha 3 for why.
    """

    def __init__(self, app):  # type: ignore[no-untyped-def]
        self._app = app

    async def __call__(self, scope, receive, send):  # type: ignore[no-untyped-def]
        scope = dict(scope)
        scope["path"] = "/"
        await self._app(scope, receive, send)


def mount_mcp(app: FastAPI) -> None:
    """Mount the Streamable HTTP MCP endpoint at /mcp on `app`.

    Call once, after `app` is constructed. Does not start the session
    manager -- see this module's docstring, gotcha 1; `app/main.py`'s own
    lifespan does that, guarded by whether this import (and ENABLE_MCP)
    succeeded at all.
    """
    sub_app = mcp.streamable_http_app()
    sub_app.add_middleware(_RequireAuth)
    # Order matters: an exact "/mcp" match must be tried before the Mount's
    # own (always-failing, for the bare path) match, or Starlette's router
    # falls through to the Mount and 307s via redirect_slashes anyway.
    app.add_route("/mcp", _ExactPathForward(sub_app), methods=["GET", "POST", "DELETE"])
    app.mount("/mcp", sub_app)


# =============================================================================
# Shared helpers
# =============================================================================


def _guard(op: str, fn, *args, **kwargs):  # type: ignore[no-untyped-def]
    """Run a tool body, logging (and translating) any denial.

    FastMCP catches an exception raised inside a tool and turns it into a
    JSON-RPC result with isError: true and HTTP 200 -- it never reaches
    app/main.py's `_path_error_handler`, so a rejected path would otherwise
    be invisible in the logs, silently weakening the observability half of
    the security model. Every tool routes its Vault calls through here so
    that can't be forgotten per-tool. A plain ValueError (a tool's own
    validation failure -- bad encoding, wrong upload seq, a commit_write
    hash mismatch, and the like, none of which are PathError) is logged the
    same way: every denial from every MCP tool shows up as `denied MCP ...`,
    not just the path-containment ones.
    """
    from . import main as appmain

    try:
        return fn(*args, **kwargs)
    except PathError as exc:
        appmain.log.warning("denied MCP %s -> %s (%s)", op, exc.code, exc.message)
        raise ValueError(f"{exc.code}: {exc.message}") from exc
    except GitOpError as exc:
        # The message already names the paths involved and the next step; it
        # is all the agent gets, since an MCP error carries only text.
        appmain.log.warning(
            "denied MCP %s -> %s (%s)", op, exc.code, exc.message.splitlines()[0] if exc.message else ""
        )
        raise ValueError(f"{exc.code}: {exc.message}") from exc
    except ValueError as exc:
        appmain.log.warning("denied MCP %s -> %s", op, exc)
        raise


def _require_write(ctx: Context) -> None:
    from . import main as appmain

    request = ctx.request_context.request
    if request is None or getattr(request.state, "token_scope", None) != "rw":
        # Same text as require_write's HTTPException (app/main.py) -- one
        # string to grep for, not two.
        raise ValueError(appmain.READONLY_MESSAGE)


def _read_all(fd: int, chunk: int = 256 * 1024) -> bytes:
    parts: list[bytes] = []
    while True:
        data = os.read(fd, chunk)
        if not data:
            break
        parts.append(data)
    return b"".join(parts)


def _presented_token(ctx: Context) -> str:
    """The bearer credential presented on this call, however it arrived."""
    request = ctx.request_context.request
    if request is None:
        return ""
    auth = request.headers.get("authorization")
    if auth and auth.startswith("Bearer "):
        return auth[7:].strip()
    return (request.headers.get("x-api-key") or "").strip()


def _token_digest(ctx: Context) -> bytes:
    return hashlib.sha256(_presented_token(ctx).encode("utf-8")).digest()


# =============================================================================
# Chunked write sessions (docs/plan-mcp-large-writes.md, "Part 2").
#
# Single process only: the image runs `uvicorn app.main:app` with no
# `--workers` (Dockerfile), so one process holds every session below. Adding
# workers would route a write_chunk call to a process that never saw the
# matching begin_write -- do not attempt to make this multi-process without
# moving session state out of this dict.
# =============================================================================

_SESSIONS: dict[str, "_UploadSession"] = {}


@dataclass
class _UploadSession:
    upload_id: str
    parts: list[str]
    encoding: str
    stack: contextlib.ExitStack
    fd: int
    committer: Any
    token_digest: bytes
    created_at: float
    lock: threading.Lock = field(default_factory=threading.Lock)
    last_activity_at: float = 0.0
    next_seq: int = 0
    bytes_received: int = 0
    chunks_received: int = 0
    sealed: bool = False
    hasher: Any = field(default_factory=hashlib.sha256)


def _reap_expired_sessions() -> None:
    """Sweep sessions idle past the TTL. Called lazily from begin_write and
    write_chunk -- no background task, nothing new in the app lifespan."""
    from . import main as appmain

    now = time.monotonic()
    ttl = appmain.CONFIG.mcp_upload_session_ttl_seconds
    for upload_id, session in list(_SESSIONS.items()):
        if now - session.last_activity_at <= ttl:
            continue
        # Non-blocking: a session mid-chunk is not expired in spirit even if
        # the clock says so; leave it for the next sweep rather than closing
        # the fd out from under an in-flight write_chunk.
        if not session.lock.acquire(blocking=False):
            continue
        try:
            if _SESSIONS.get(upload_id) is session:
                del _SESSIONS[upload_id]
                session.stack.close()
                appmain.log.info(
                    "reaped expired MCP upload session %s (%s, idle %.0fs)",
                    upload_id, "/".join(session.parts), now - session.last_activity_at,
                )
        finally:
            session.lock.release()


def active_upload_paths() -> list[str]:
    """Paths of open chunked-write sessions, for git_pull/git_merge's
    uploads_in_flight check. Sweeps expired ones first so a session its owner
    abandoned does not block pulls forever."""
    _reap_expired_sessions()
    return ["/".join(sess.parts) for sess in list(_SESSIONS.values())]


# =============================================================================
# Tools -- one per REST capability (docs/plan-mcp-connector.md, "Tool
# surface"). find_files is deliberately not included yet: the plan's open
# question 5 defers it until list_files(recursive=True) proves clumsy in
# real use, since removing a tool from a published connector is harder than
# adding one later.
# =============================================================================


@mcp.tool()
def get_folder_info(ctx: Context) -> dict[str, Any]:
    """Server limits, policy, and this token's scope. Call this first:
    `writes_allowed` is false for a read-only token, in which case every
    write tool will be refused, so don't attempt one. It also reports the
    inline size budgets read_file and write_file operate under, and the
    chunk size begin_write/write_chunk advise."""
    from . import main as appmain

    request = ctx.request_context.request
    scope = getattr(request.state, "token_scope", "ro") if request is not None else "ro"
    git_ro_tools = ", git_status, git_log, git_diff" if appmain.CONFIG.enable_git_read else ""
    info: dict[str, Any] = {
        "max_upload_bytes": appmain.CONFIG.max_upload_bytes,
        "max_list_entries": appmain.CONFIG.max_list_entries,
        "denied_names": sorted(appmain.CONFIG.denied_names),
        "allow_delete": appmain.CONFIG.allow_delete,
        "allow_hardlinked_files": appmain.CONFIG.allow_hardlinked_files,
        "token_scope": scope,
        "writes_allowed": scope == "rw",
        "token_scope_note": (
            "rw: every tool works. ro: list_files, stat_file, read_file, "
            f"request_download, download_status, get_folder_info and upload_status{git_ro_tools} work; every write tool is refused "
            "with a 'read-only' error. With an ro token, don't attempt writes -- "
            "tell the user this task needs a read-write token."
        ),
        "mcp_inline_limit_chars": appmain.CONFIG.mcp_inline_limit_chars,
        "mcp_base64_limit_bytes": appmain.CONFIG.mcp_base64_limit_bytes,
        "mcp_base64_limit_note": (
            "Advisory, not a hard budget the server can see: the real ceiling on a "
            "single write_file/read_file call is whatever this caller can emit or hold "
            "in one tool result, which varies by model and context already spent. For "
            "anything over a few KB, use begin_write/write_chunk/commit_write instead of "
            "one inline call -- write_file itself refuses and says so once content is "
            "over mcp_base64_limit_bytes decoded."
        ),
        "mcp_upload_chunk_advisory_bytes": appmain.CONFIG.mcp_upload_chunk_advisory_bytes,
        "mcp_pdf_max_bytes": appmain.CONFIG.mcp_pdf_max_bytes,
        "read_file_note": (
            "read_file reads PDFs as text via pages=\"1-3\" (extraction limited to PDFs up "
            "to mcp_pdf_max_bytes) and reads large text files in pieces via offset/length. "
            "For a whole PDF, an image, or any binary file use request_download."
        ),
        "upload_urls": {
            "ticket_ttl_seconds": appmain.CONFIG.upload_ticket_ttl_seconds,
            "note": (
                "PREFERRED for any file that already exists on your machine: call "
                "request_upload(path), run the curl_command it returns with your "
                "local path filled in, and compare the sha256 curl prints with "
                "sha256sum of your file. The bytes never pass through a tool call, "
                "and you need no token in your shell."
            ),
        },
        "download_urls": {
            "ticket_ttl_seconds": appmain.CONFIG.download_ticket_ttl_seconds,
            "note": (
                "PREFERRED for any binary file (PDF, image, audio) or any text file "
                "too big for read_file's inline budget: call request_download(path), "
                "run the curl_command it returns with an output path filled in, and "
                "compare sha256sum of the result with the sha256 it returned. Works "
                "with a read-only token."
            ),
        },
        "bridge": {
            "available": appmain._bridge_script_path() is not None,
            "download_url": "/v1/bridge.py",
            "note": (
                "Prefer upload_urls (above) when you have a shell. "
                "If you can run shell commands or launch a local MCP server, do NOT "
                "base64 a binary file through write_file or write_chunk -- your own "
                "output ceiling is the binding limit and it is well under 10 KB. "
                "Fetch /v1/bridge.py with your read token and run "
                "`python3 claude_files_bridge.py upload <local> <remote>`; it streams "
                "the bytes straight from disk and verifies the sha256. "
                "begin_write/write_chunk remain the answer only where no shell and no "
                "local process are available."
            ),
        },
    }
    info["git"] = appmain.GIT.info_block() if appmain.GIT is not None else GitRepo.disabled_info()
    info["extensions"] = _ext.info_block(_EXTENSIONS)
    return info


@mcp.tool()
def list_files(path: str = "", recursive: bool = False, limit: int = 1000) -> dict[str, Any]:
    """List a directory. `recursive=true` walks all descendants. `readable`
    on each entry is false for symlinks and other non-regular files --
    those are listed for completeness but refused on read."""
    from . import main as appmain

    def _do():  # type: ignore[no-untyped-def]
        parts = appmain.VAULT.components(path)
        cap = min(limit, appmain.CONFIG.max_list_entries)
        entries = (
            appmain.VAULT.walk(parts, max_entries=cap)
            if recursive
            else appmain.VAULT.listdir(parts)
        )
        return {
            "path": "/".join(parts),
            "entries": [
                {
                    "name": e.name, "path": e.path, "type": e.type,
                    "size": e.size, "mtime": e.mtime, "readable": e.readable,
                }
                for e in entries[:cap]
            ],
            "truncated": len(entries) >= cap,
        }

    return _guard("list_files", _do)


@mcp.tool()
def stat_file(path: str) -> dict[str, Any]:
    """Type, size, and mtime for one path."""
    from . import main as appmain

    def _do():  # type: ignore[no-untyped-def]
        e = appmain.VAULT.stat(appmain.VAULT.components(path))
        return {
            "name": e.name, "path": e.path, "type": e.type,
            "size": e.size, "mtime": e.mtime, "readable": e.readable,
        }

    return _guard("stat_file", _do)


_PDF_MAX_PAGES_PER_CALL = 50


def _parse_page_spec(spec: str, page_count: int) -> list[int]:
    """'3', '2-5', '1,3,7-9', '5-' (to the end) -> sorted unique 1-based pages."""
    pages: set[int] = set()
    for token in spec.split(","):
        token = token.strip()
        if not token:
            continue
        try:
            if "-" in token:
                lo_s, _, hi_s = token.partition("-")
                lo = int(lo_s)
                hi = int(hi_s) if hi_s.strip() else page_count
            else:
                lo = hi = int(token)
        except ValueError:
            raise ValueError(
                f"Bad pages value {spec!r}: use page numbers like '3', '2-5', '1,3,7-9' or '5-'"
            ) from None
        if lo < 1 or hi < lo or hi > page_count:
            raise ValueError(
                f"pages {token!r} is out of range: this PDF has {page_count} page(s), "
                f"numbered from 1"
            )
        pages.update(range(lo, hi + 1))
    if not pages:
        raise ValueError(f"Bad pages value {spec!r}: no page numbers given")
    return sorted(pages)


def _format_pages(pages: list[int]) -> str:
    runs: list[str] = []
    start = prev = pages[0]
    for n in pages[1:] + [None]:  # type: ignore[list-item]
        if n is not None and n == prev + 1:
            prev = n
            continue
        runs.append(str(start) if start == prev else f"{start}-{prev}")
        if n is not None:
            start = prev = n
    return ",".join(runs)


def _read_pdf(data: bytes, rel: str, pages: str | None, limit: int, digest: str) -> dict[str, Any]:
    from . import main as appmain

    cap = appmain.CONFIG.mcp_pdf_max_bytes
    if len(data) > cap:
        raise ValueError(
            f"'{rel}' is {len(data):,} bytes, over the {cap:,}-byte PDF text-extraction "
            f"limit. Download it with request_download('{rel}') and run the curl command it returns."
        )
    try:
        from pypdf import PdfReader
    except ImportError:
        raise ValueError(
            f"PDF text extraction is not installed on this server. Download '{rel}' with "
            f"request_download('{rel}') and run the curl command it returns."
        ) from None

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            raise ValueError(
                f"'{rel}' is password-protected. Download it with request_download('{rel}')."
            )
        page_count = len(reader.pages)
        selected = _parse_page_spec(pages, page_count) if pages else list(range(1, page_count + 1))

        chunks: list[str] = []
        used = 0
        done: list[int] = []
        truncated_page: int | None = None
        empty: list[int] = []
        for n in selected:
            if len(done) >= _PDF_MAX_PAGES_PER_CALL:
                break
            text = (reader.pages[n - 1].extract_text() or "").strip()
            if not text:
                empty.append(n)
            piece = f"--- page {n} ---\n{text}\n"
            if used + len(piece) > limit:
                if done:
                    break
                piece = piece[:limit]
                truncated_page = n
            chunks.append(piece)
            used += len(piece)
            done.append(n)
            if truncated_page is not None:
                break
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(
            f"'{rel}' could not be parsed as a PDF ({type(exc).__name__}). Download it "
            f"with request_download('{rel}') and run the curl command it returns."
        ) from None

    remaining = [n for n in selected if n > done[-1]]
    out: dict[str, Any] = {
        "text": "".join(chunks),
        "format": "pdf",
        "page_count": page_count,
        "pages_returned": _format_pages(done),
        "bytes": len(data),
        "sha256": digest,
    }
    if remaining:
        out["next_pages"] = _format_pages(remaining)
        out["truncated"] = True
        out["note"] = (
            f"Stopped at the {limit:,}-character / {_PDF_MAX_PAGES_PER_CALL}-page budget for "
            f"one call. Pass pages='{out['next_pages']}' to continue, or get the whole file "
            f"with request_download('{rel}')."
        )
    if truncated_page is not None:
        out["truncated"] = True
        out["note"] = (
            f"Page {truncated_page} alone exceeds the {limit:,}-character budget and was cut "
            f"off. " + out.get("note", "") + f" Use request_download('{rel}') for the full page."
        )
    if empty:
        out["pages_without_text"] = _format_pages(empty)
        out["empty_note"] = (
            "These pages have no extractable text (likely scanned images); "
            f"request_download('{rel}') to view them."
        )
    return out


@mcp.tool()
def read_file(
    path: str,
    encoding: str = "utf-8",
    max_chars: int | None = None,
    offset: int | None = None,
    length: int | None = None,
    pages: str | None = None,
) -> dict[str, Any]:
    """Read a file's contents. `encoding="utf-8"` (default) decodes the file
    as text; a file containing a NUL byte or that fails UTF-8 decoding is
    refused with an explanation, never returned as mojibake or silently
    base64'd. Pass `encoding="base64"` for a small binary file (capped well
    below the text budget -- see get_folder_info).

    PDFs: pass `pages` ("3", "2-5", "1,3,7-9", "5-") to get the extracted
    text of those pages (1-based); with no `pages` you get from page 1 until
    the per-call budget is used, plus `next_pages` to pass back in. Scanned
    PDFs have no text layer -- those pages are listed in `pages_without_text`.

    Large text: pass `offset` (characters from the start, default 0) and/or
    `length` (characters, capped at the inline budget) to read a file in
    pieces; the result has `total_chars` and `next_offset` (null at the end).
    Without them, a result over the inline character budget (`max_chars`, or
    the server default) comes back as {preview, bytes, truncated: true, note}
    -- never a silent cut.

    For the whole of a PDF, an image, or any big file, use request_download
    instead. Every successful result includes `bytes` and `sha256` for the
    whole file, so a copy can be verified by forwarding them straight into
    write_file's `expect_bytes`/`expect_sha256` without computing anything
    yourself."""
    from . import main as appmain

    def _do():  # type: ignore[no-untyped-def]
        parts = appmain.VAULT.components(path)
        rel = "/".join(parts)
        fd, _st = appmain.VAULT.open_read(parts)
        try:
            data = _read_all(fd)
        finally:
            os.close(fd)
        digest = hashlib.sha256(data).hexdigest()

        if encoding == "base64" and (offset is not None or length is not None or pages):
            raise ValueError("offset, length and pages apply to encoding='utf-8' reads only")

        if encoding == "base64":
            cap = appmain.CONFIG.mcp_base64_limit_bytes
            if len(data) > cap:
                raise ValueError(
                    f"'{rel}' is {len(data):,} bytes, over the {cap:,}-byte base64 "
                    f"limit for this connector. Download it with "
                    f"request_download('{rel}') and run the curl command it returns."
                )
            return {
                "content": base64.b64encode(data).decode("ascii"),
                "encoding": "base64",
                "bytes": len(data),
                "sha256": digest,
            }

        if encoding != "utf-8":
            raise ValueError(f"Unknown encoding {encoding!r}; expected 'utf-8' or 'base64'")

        read_limit = appmain.CONFIG.mcp_inline_limit_chars
        if max_chars is not None:
            read_limit = min(read_limit, max_chars)

        if data.startswith(b"%PDF-"):
            if offset is not None or length is not None:
                raise ValueError(
                    "offset/length are for text files; select PDF content with pages "
                    "(e.g. pages='1-3')"
                )
            return _read_pdf(data, rel, pages, read_limit, digest)
        if pages:
            raise ValueError(f"'{rel}' is not a PDF, so pages does not apply")

        if b"\x00" in data:
            raise ValueError(
                f"'{rel}' contains a NUL byte and cannot be decoded as UTF-8 text "
                f"({len(data):,} bytes). Use encoding=\"base64\" if it fits that "
                f"budget, or call request_download('{rel}') and run the curl command it returns."
            )
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            raise ValueError(
                f"'{rel}' is not valid UTF-8 and cannot be read as text "
                f"({len(data):,} bytes). Use encoding=\"base64\" if it fits that "
                f"budget, or call request_download('{rel}') and run the curl command it returns."
            ) from None

        limit = read_limit
        if offset is not None or length is not None:
            start = 0 if offset is None else offset
            if start < 0 or (length is not None and length <= 0):
                raise ValueError("offset must be >= 0 and length must be > 0")
            if start > len(text):
                raise ValueError(
                    f"offset {start:,} is past the end of '{rel}' ({len(text):,} characters)"
                )
            want = limit if length is None else min(length, limit)
            end = min(start + want, len(text))
            return {
                "text": text[start:end],
                "offset": start,
                "returned_chars": end - start,
                "total_chars": len(text),
                "next_offset": end if end < len(text) else None,
                "bytes": len(data),
                "sha256": digest,
            }
        if len(text) <= limit:
            return {"text": text, "bytes": len(data), "sha256": digest}

        return {
            "preview": text[:_PREVIEW_CHARS],
            "bytes": len(data),
            "sha256": digest,
            "truncated": True,
            "note": (
                f"'{rel}' is {len(data):,} bytes, over the {limit:,}-character inline "
                f"budget for this connector. Only the preview above is included; "
                f"read it in pieces with offset/length (next offset: {_PREVIEW_CHARS}), or get the "
                f"whole file with request_download('{rel}') and the curl command it returns."
            ),
        }

    return _guard("read_file", _do)


@mcp.tool()
@_git_note
def write_file(
    path: str,
    content: str,
    ctx: Context,
    encoding: str = "utf-8",
    parents: bool = True,
    overwrite: bool = True,
    expect_bytes: int | None = None,
    expect_sha256: str | None = None,
) -> dict[str, Any]:
    """Create or replace a file from content you are composing in this call
    -- a short report, generated text. If the content already exists as a
    local file, use request_upload instead: it sends the bytes over HTTP
    rather than through your own output, which is faster and doesn't corrupt
    large files. The result carries the server-computed `size` and `sha256`.
    `parents` defaults to true here (unlike the
    REST API): a model that has just been told a path doesn't exist isn't
    helped by a 409, so the missing parent chain is created automatically.
    `overwrite=false` refuses if the file already exists. `encoding="utf-8"`
    (default) treats `content` as text; `encoding="base64"` decodes it as
    binary, capped well below the text budget (see get_folder_info) -- over
    that cap, this call is refused naming begin_write/write_chunk/commit_write
    for a file that size. `expect_bytes` and/or `expect_sha256` are checked
    against the decoded content BEFORE anything is written: a mismatch means
    nothing is created, turning a call truncated in transit into a clean
    refusal instead of a silently short file. Required (at least one) for a
    base64 write over 1 KB decoded -- read_file's result on a source file
    already carries both, so a copy can forward them without computing
    anything. Writes are atomic -- a reader never sees a half-written file.
    Requires a read-write token. {git_note}"""
    from . import main as appmain

    _require_write(ctx)

    def _do():  # type: ignore[no-untyped-def]
        if encoding == "utf-8":
            data = content.encode("utf-8")
        elif encoding == "base64":
            try:
                data = base64.b64decode(content, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ValueError(f"content is not valid base64: {exc}") from None
            cap = appmain.CONFIG.mcp_base64_limit_bytes
            if len(data) > cap:
                raise ValueError(
                    f"Decoded content is {len(data):,} bytes, over the {cap:,}-byte "
                    f"single-shot base64 limit for this connector. If you have a "
                    f"shell, call request_upload('{path}') and run the curl command it "
                    f"returns. Or, if you can run a local MCP server, fetch /v1/bridge.py with "
                    f"your read token and run `python3 claude_files_bridge.py upload "
                    f"<local> {path}` instead -- it streams the bytes straight from "
                    f"disk, with no output ceiling. Otherwise use "
                    f"begin_write/write_chunk/commit_write for a file this size, or "
                    f"PUT /v1/content/{{path}} over REST."
                )
            if len(data) > 1024 and expect_bytes is None and expect_sha256 is None:
                raise ValueError(
                    "A base64 write over 1 KB decoded requires expect_bytes and/or "
                    "expect_sha256, so a call truncated in transit is caught before "
                    "anything is written rather than silently producing a short file. "
                    "If the content came from read_file, its result already carries both."
                )
        else:
            raise ValueError(f"Unknown encoding {encoding!r}; expected 'utf-8' or 'base64'")

        # Verify BEFORE atomic_write is entered: on a mismatch nothing is
        # created, so there is no corrupt file to notice and clean up after.
        if expect_bytes is not None and expect_bytes != len(data):
            raise ValueError(
                f"Received {len(data):,} bytes but expect_bytes said {expect_bytes:,} -- "
                f"the content was almost certainly truncated in transit. Nothing was "
                f"written. For a file this size, use begin_write/write_chunk/commit_write "
                f"instead."
            )
        if expect_sha256 is not None:
            digest = hashlib.sha256(data).hexdigest()
            if expect_sha256.lower() != digest:
                raise ValueError(
                    f"Computed sha256 {digest} does not match expect_sha256 "
                    f"{expect_sha256!r} -- the content was almost certainly truncated or "
                    f"altered in transit. Nothing was written. For a file this size, use "
                    f"begin_write/write_chunk/commit_write instead."
                )

        parts = appmain.VAULT.components(path)
        if not parts:
            raise PathError(403, "is_root", "Cannot write to the root itself")
        with appmain.VAULT.atomic_write(
            parts, overwrite=overwrite, create_parents=parents
        ) as (fd, committer):
            os.write(fd, data)
            committer.commit(fd)
        appmain.log.info("wrote %s (%d bytes) via MCP", "/".join(parts), len(data))
        return {
            "ok": True, "path": "/".join(parts), "size": len(data), "created": True,
            "sha256": hashlib.sha256(data).hexdigest(),
        }

    return _guard("write_file", _do)


@mcp.tool()
def begin_write(
    path: str,
    ctx: Context,
    encoding: str = "utf-8",
    overwrite: bool = True,
    parents: bool = True,
) -> dict[str, Any]:
    """LAST RESORT -- only for callers with no shell. If you have a shell,
    use request_upload instead: this path routes every byte through your own
    output, which is slow and error-prone for anything over a few KB.
    Start a chunked write session for a file too large for write_file's
    single-shot base64 cap (or just too large to comfortably emit in one
    tool call as text). Returns an `upload_id`; follow with write_chunk
    calls (seq=0, 1, 2, ... with no gaps) and finish with commit_write,
    which is the only call that actually creates or replaces the
    destination -- the file does not exist at this path, under this name,
    until commit_write succeeds. `encoding` is fixed for the session: pass
    `encoding="base64"` for binary content, sliced only on 4-character
    boundaries (see write_chunk); `encoding="utf-8"` (default) for text,
    never splitting a multi-byte character across chunks. `overwrite=false`
    is checked now, at begin_write -- a file created at this path during the
    session is still overwritten at commit_write, since re-checking there
    would not close that race, only move it. Requires a read-write token."""
    from . import main as appmain

    _require_write(ctx)

    def _do():  # type: ignore[no-untyped-def]
        if encoding not in ("utf-8", "base64"):
            raise ValueError(f"Unknown encoding {encoding!r}; expected 'utf-8' or 'base64'")

        parts = appmain.VAULT.components(path)
        if not parts:
            raise PathError(403, "is_root", "Cannot write to the root itself")

        _reap_expired_sessions()

        if len(_SESSIONS) >= appmain.CONFIG.mcp_max_concurrent_uploads:
            raise ValueError(
                f"Too many concurrent chunked uploads "
                f"({appmain.CONFIG.mcp_max_concurrent_uploads} allowed). Finish or "
                f"abort_write an existing one first."
            )

        stack = contextlib.ExitStack()
        try:
            fd, committer = stack.enter_context(
                appmain.VAULT.atomic_write(parts, overwrite=overwrite, create_parents=parents)
            )
        except BaseException:
            stack.close()
            raise

        upload_id = secrets.token_urlsafe(16)
        now = time.monotonic()
        session = _UploadSession(
            upload_id=upload_id,
            parts=parts,
            encoding=encoding,
            stack=stack,
            fd=fd,
            committer=committer,
            token_digest=_token_digest(ctx),
            created_at=now,
            last_activity_at=now,
        )
        _SESSIONS[upload_id] = session
        appmain.log.info(
            "begin_write %s (%s) via MCP -> upload_id %s", "/".join(parts), encoding, upload_id
        )
        expires_at = (
            datetime.now(timezone.utc)
            + timedelta(seconds=appmain.CONFIG.mcp_upload_session_ttl_seconds)
        ).isoformat()
        return {
            "upload_id": upload_id,
            "path": "/".join(parts),
            "chunk_advisory_bytes": appmain.CONFIG.mcp_upload_chunk_advisory_bytes,
            "expires_at": expires_at,
            "note": (
                "Call write_chunk with seq=0, then 1, 2, ... in order (no gaps, "
                "resending a seq after a failed call is fine). Finish with "
                "commit_write, which requires expect_bytes and/or expect_sha256. "
                "Nothing is written to this path until commit_write succeeds."
            ),
        }

    return _guard("begin_write", _do)


@mcp.tool()
def write_chunk(upload_id: str, seq: int, content: str, ctx: Context) -> dict[str, Any]:
    """Append one chunk to a session opened by begin_write. `seq` must equal
    the number of chunks already received, starting at 0 -- not "greater
    than", not "not yet seen", exactly equal. A gap, a repeat, or an
    out-of-order seq is refused; resending the same seq after a failed call
    is the correct retry and succeeds. For encoding="base64" (fixed at
    begin_write), `content` must be a standalone-decodable base64 string
    whose length is a multiple of 4 characters -- split source bytes only on
    a 4-character (3-byte) boundary, which is what makes each chunk decode
    independently without buffering the whole file. Once a chunk carrying
    '=' padding is accepted, this upload is sealed: no further write_chunk
    calls are accepted, since padding marks the end of the base64 stream --
    call commit_write next. For encoding="utf-8", `content` is UTF-8 encoded
    and appended as-is; do not split a multi-byte character across chunks.
    Requires a read-write token, and the same credential that opened the
    session."""
    from . import main as appmain

    _require_write(ctx)

    def _do():  # type: ignore[no-untyped-def]
        _reap_expired_sessions()
        session = _SESSIONS.get(upload_id)
        if session is None:
            raise ValueError(
                f"Unknown or expired upload_id {upload_id!r}. It may have been "
                f"committed, aborted, or reaped after being idle past the TTL. Start "
                f"over with begin_write."
            )
        if not hmac.compare_digest(session.token_digest, _token_digest(ctx)):
            raise ValueError("This upload_id was opened with a different credential")

        if not session.lock.acquire(blocking=False):
            raise ValueError(
                f"A chunk is already in flight for upload {upload_id!r}. Wait for it "
                f"to finish before sending the next one -- concurrent chunks on one "
                f"upload are refused rather than silently serialised."
            )
        try:
            if session.sealed:
                raise ValueError(
                    "This upload already received a chunk carrying base64 padding "
                    "('='), which marks the end of the stream. No further chunks are "
                    "accepted -- call commit_write, or abort_write to discard it."
                )
            if seq != session.next_seq:
                raise ValueError(
                    f"Expected seq={session.next_seq} for this upload "
                    f"({session.chunks_received} chunk(s) received so far), got "
                    f"seq={seq}. Chunks must arrive in order with no gaps. Resend "
                    f"seq={session.next_seq}."
                )

            if session.encoding == "base64":
                if len(content) % 4 != 0:
                    raise ValueError(
                        f"Chunk is {len(content)} base64 characters, not a multiple of "
                        f"4. Split source bytes only on a 4-character boundary (3 "
                        f"decoded bytes) so each chunk decodes standalone."
                    )
                try:
                    data = base64.b64decode(content, validate=True)
                except (binascii.Error, ValueError) as exc:
                    raise ValueError(
                        f"Chunk is not valid standalone base64: {exc}. Split source "
                        f"bytes only on a 4-character boundary."
                    ) from None
                sealed_now = "=" in content
            else:
                data = content.encode("utf-8")
                sealed_now = False

            new_total = session.bytes_received + len(data)
            cap = appmain.CONFIG.max_upload_bytes
            if new_total > cap:
                raise ValueError(
                    f"This upload would total {new_total:,} bytes, over "
                    f"MAX_UPLOAD_BYTES ({cap:,}). {session.bytes_received:,} bytes "
                    f"received so far; this chunk was not written. abort_write and "
                    f"retry, or send a smaller remaining chunk."
                )

            os.write(session.fd, data)
            session.hasher.update(data)
            session.bytes_received = new_total
            session.chunks_received += 1
            session.next_seq += 1
            session.last_activity_at = time.monotonic()
            if sealed_now:
                session.sealed = True
            return {
                "seq": seq,
                "chunks_received": session.chunks_received,
                "bytes_received": session.bytes_received,
            }
        finally:
            session.lock.release()

    return _guard("write_chunk", _do)


@mcp.tool()
def commit_write(
    upload_id: str,
    ctx: Context,
    expect_bytes: int | None = None,
    expect_sha256: str | None = None,
) -> dict[str, Any]:
    """Finish a chunked upload: verify it, then rename the temp file into
    place -- the one moment the destination path actually changes, and the
    same atomic rename every other write in this API uses, so a reader
    never sees a partial file. Requires at least one of expect_bytes /
    expect_sha256 -- refused without either, since anything worth chunking
    is worth verifying and by the time an upload has taken several
    write_chunk calls there is no excuse for not knowing how many bytes
    were meant. A mismatch leaves nothing written at the destination and
    the session open: fix the discrepancy and retry commit_write, or
    abort_write to give up. Requires a read-write token, and the same
    credential that opened the session."""
    from . import main as appmain

    _require_write(ctx)

    def _do():  # type: ignore[no-untyped-def]
        _reap_expired_sessions()
        session = _SESSIONS.get(upload_id)
        if session is None:
            raise ValueError(
                f"Unknown or expired upload_id {upload_id!r}. It may have been "
                f"committed, aborted, or reaped after being idle past the TTL. Start "
                f"over with begin_write."
            )
        if not hmac.compare_digest(session.token_digest, _token_digest(ctx)):
            raise ValueError("This upload_id was opened with a different credential")

        if not session.lock.acquire(blocking=False):
            raise ValueError(f"A chunk is already in flight for upload {upload_id!r}.")
        try:
            if expect_bytes is None and expect_sha256 is None:
                raise ValueError(
                    "commit_write requires expect_bytes and/or expect_sha256. Nothing "
                    "was committed."
                )

            digest = session.hasher.hexdigest()
            if expect_bytes is not None and expect_bytes != session.bytes_received:
                raise ValueError(
                    f"Received {session.bytes_received:,} bytes but expect_bytes said "
                    f"{expect_bytes:,} -- the upload was almost certainly incomplete. "
                    f"Nothing was written; the session is still open. Send the missing "
                    f"bytes and retry commit_write, or abort_write to give up."
                )
            if expect_sha256 is not None and expect_sha256.lower() != digest:
                raise ValueError(
                    f"Computed sha256 {digest} does not match expect_sha256 "
                    f"{expect_sha256!r}. Nothing was written; the session is still "
                    f"open. Fix the content and retry commit_write, or abort_write to "
                    f"give up."
                )

            session.committer.commit(session.fd)
            del _SESSIONS[upload_id]
            session.stack.close()
            appmain.log.info(
                "committed chunked write %s (%d bytes, %d chunks) via MCP",
                "/".join(session.parts), session.bytes_received, session.chunks_received,
            )
            return {
                "ok": True,
                "path": "/".join(session.parts),
                "size": session.bytes_received,
                "sha256": digest,
                "created": True,
            }
        finally:
            session.lock.release()

    return _guard("commit_write", _do)


@mcp.tool()
def abort_write(upload_id: str, ctx: Context) -> dict[str, Any]:
    """Cancel a chunked upload, discarding whatever was written so far --
    the temp file is removed and the destination path is left untouched
    (it was never created; commit_write is the only call that creates it).
    Safe to call twice, or on an unknown/already-finished upload_id: both
    return {"ok": true, "aborted": false} rather than raising, so cleanup
    code never gets a second error for its trouble. Requires a read-write
    token, and the same credential that opened the session."""
    from . import main as appmain

    _require_write(ctx)

    def _do():  # type: ignore[no-untyped-def]
        session = _SESSIONS.get(upload_id)
        if session is None:
            return {"ok": True, "aborted": False}
        if not hmac.compare_digest(session.token_digest, _token_digest(ctx)):
            raise ValueError("This upload_id was opened with a different credential")

        session = _SESSIONS.pop(upload_id, None)
        if session is None:
            return {"ok": True, "aborted": False}
        with session.lock:
            session.stack.close()
        appmain.log.info("aborted chunked write %s via MCP", "/".join(session.parts))
        return {"ok": True, "aborted": True}

    return _guard("abort_write", _do)


@mcp.tool()
@_git_note
def delete_file(path: str, ctx: Context, recursive: bool = False) -> dict[str, Any]:
    """Delete a file, or a directory (with recursive=true for a non-empty
    one). Honours ALLOW_DELETE -- refused entirely if the server disables
    deletion. Requires a read-write token. {git_note}"""
    from . import main as appmain

    _require_write(ctx)

    def _do():  # type: ignore[no-untyped-def]
        if not appmain.CONFIG.allow_delete:
            raise PathError(403, "delete_disabled", "Deletion is disabled on this server")
        parts = appmain.VAULT.components(path)
        if not parts:
            raise PathError(403, "is_root", "Cannot delete the root directory")
        appmain.VAULT.delete(parts, recursive=recursive)
        appmain.log.info("deleted %s (recursive=%s) via MCP", "/".join(parts), recursive)
        return {"ok": True, "path": "/".join(parts)}

    return _guard("delete_file", _do)


@mcp.tool()
def make_directory(path: str, ctx: Context, parents: bool = True) -> dict[str, Any]:
    """Create a directory. Idempotent: creating one that already exists
    returns created: false, not an error. Requires a read-write token."""
    from . import main as appmain

    _require_write(ctx)

    def _do():  # type: ignore[no-untyped-def]
        parts = appmain.VAULT.components(path)
        if not parts:
            raise PathError(403, "is_root", "Cannot create the root directory")
        created = appmain.VAULT.mkdir(parts, parents=parents)
        return {"ok": True, "path": "/".join(parts), "created": created}

    return _guard("make_directory", _do)


@mcp.tool()
def move_file(
    src: str, dst: str, ctx: Context, overwrite: bool = False, parents: bool = False
) -> dict[str, Any]:
    """Move or rename a file within the shared folder. `parents=true`
    creates dst's parent directory chain first. Requires a read-write
    token."""
    from . import main as appmain

    _require_write(ctx)

    def _do():  # type: ignore[no-untyped-def]
        src_parts = appmain.VAULT.components(src)
        dst_parts = appmain.VAULT.components(dst)
        appmain.VAULT.move(src_parts, dst_parts, overwrite=overwrite, create_parents=parents)
        appmain.log.info(
            "moved %s -> %s via MCP", "/".join(src_parts), "/".join(dst_parts)
        )
        return {"ok": True, "path": "/".join(dst_parts)}

    return _guard("move_file", _do)


@mcp.tool()
def copy_file(
    src: str, dst: str, ctx: Context, overwrite: bool = False, parents: bool = False
) -> dict[str, Any]:
    """Copy a file within the shared folder, server-side -- the bytes never
    pass through this tool call, so there is no size ceiling and no output
    budget spent, unlike reading a file out and writing it back in with
    read_file/write_file. Useful for re-archiving or duplicating a file
    that's already in the folder. `overwrite=false` (default) refuses if
    `dst` already exists; `parents=true` creates dst's parent directory
    chain first. `src` must be a regular file (not a directory or symlink).
    Requires a read-write token."""
    from . import main as appmain

    _require_write(ctx)

    def _do():  # type: ignore[no-untyped-def]
        src_parts = appmain.VAULT.components(src)
        dst_parts = appmain.VAULT.components(dst)
        size = appmain.VAULT.copy(
            src_parts, dst_parts, overwrite=overwrite, create_parents=parents
        )
        appmain.log.info(
            "copied %s -> %s (%d bytes) via MCP", "/".join(src_parts), "/".join(dst_parts), size
        )
        return {"ok": True, "path": "/".join(dst_parts), "size": size, "created": True}

    return _guard("copy_file", _do)


# =============================================================================
# One-time upload URLs -- docs/upload-urls.md
# =============================================================================


@mcp.tool()
def request_upload(
    path: str,
    ctx: Context,
    overwrite: bool = True,
    parents: bool = True,
    expect_sha256: str | None = None,
) -> dict[str, Any]:
    """PREFERRED way to upload any file that already exists on your machine,
    of any size or type. Do NOT paste its contents into write_file or
    write_chunk: that routes every byte through your own output, which is
    slow and corrupts large files. Instead, this returns a single-use
    `upload_url` scoped to `path`; run the returned `curl_command` from your
    shell with <LOCAL_FILE> replaced by your local file's path. The bytes go
    straight over HTTP, and curl prints the server's `size` and `sha256` for
    them -- compare that with `sha256sum <LOCAL_FILE>` (or `shasum -a 256`).
    `upload_status(upload_id)` reports the same result, or progress while a
    large upload is still streaming. The URL expires if unused (see
    `expires_at`) and works exactly once; request a new one to retry. No
    bearer token is needed for the curl -- the URL itself is the
    authorization, so don't paste it anywhere else. `expect_sha256` is
    optional; if given, the upload is rejected and nothing is written unless
    the bytes match. Requires a read-write token and a shell with outbound
    network access to this server."""
    from . import main as appmain

    _require_write(ctx)

    def _do():  # type: ignore[no-untyped-def]
        resp = appmain.issue_upload_ticket(
            path,
            overwrite=overwrite,
            parents=parents,
            max_bytes=None,
            expect_sha256=expect_sha256,
            request=ctx.request_context.request,
        )
        out = resp.model_dump()
        out["next_steps"] = (
            "1) Run curl_command with <LOCAL_FILE> replaced by your local path. "
            "2) Compare the sha256 in curl's output with sha256sum of your local file. "
            "If curl's output was lost, call upload_status with this upload_id."
        )
        return out

    return _guard("request_upload", _do)


@mcp.tool()
def upload_status(upload_id: str) -> dict[str, Any]:
    """Status of a request_upload ticket: `state` is pending (not used yet),
    receiving (bytes still streaming; see bytes_received), complete (see
    `size` and `sha256` -- compare with your local sha256sum), failed (see
    `error`; request a new upload), or expired (request a new upload)."""
    from . import main as appmain

    return _guard("upload_status", lambda: appmain.upload_ticket_status(upload_id).model_dump())


# =============================================================================
# One-time download URLs -- docs/download-urls.md
# =============================================================================


@mcp.tool()
def request_download(path: str, ctx: Context) -> dict[str, Any]:
    """PREFERRED way to read any file that read_file cannot return inline:
    PDFs, images, audio, other binary files, and text files over the inline
    budget. Returns a single-use `download_url` scoped to `path`, the file's
    `size` and `sha256`, and a `curl_command`; run it from your shell with
    <OUTPUT_FILE> replaced by where to save the file, then check that
    `sha256sum <OUTPUT_FILE>` (or `shasum -a 256`) equals the returned
    `sha256`. The bytes go straight over HTTP and never pass through a tool
    result. The URL expires if unused (see `expires_at`) and works exactly
    once; request a new one to retry. No bearer token is needed for the curl
    -- the URL itself is the authorization, so don't paste it anywhere else.
    `download_status(download_id)` reports progress or the outcome. Works with
    a read-only token. Needs a shell with outbound network access to this
    server."""
    from . import main as appmain

    def _do():  # type: ignore[no-untyped-def]
        resp = appmain.issue_download_ticket(path, request=ctx.request_context.request)
        out = resp.model_dump()
        out["next_steps"] = (
            "1) Run curl_command with <OUTPUT_FILE> replaced by your local path. "
            "2) Confirm sha256sum of that file equals the sha256 returned here. "
            "If curl failed partway, request a new download."
        )
        return out

    return _guard("request_download", _do)


@mcp.tool()
def download_status(download_id: str) -> dict[str, Any]:
    """Status of a request_download ticket: `state` is pending (not used yet),
    streaming (see bytes_sent), complete, failed (see `error`; request a new
    download), or expired (request a new download)."""
    from . import main as appmain

    return _guard("download_status", lambda: appmain.download_ticket_status(download_id).model_dump())


# =============================================================================
# Git tools -- docs/plan-mcp-git-ops.md
#
# Registered only for the enabled class (ENABLE_GIT_READ / ENABLE_GIT_WRITE), so
# a disabled tool is absent from tools/list rather than present and refusing.
#
# These are `async def`, unlike every tool above. FastMCP calls a sync tool
# inline on the event loop, and a git push can take a minute: run in a worker
# thread so the server keeps answering health checks and other requests.
# `_guard` is sync, so it runs inside the thread; the read-write check runs
# first, in the coroutine, where the request context is.
# =============================================================================


def _git() -> GitRepo:
    from . import main as appmain

    assert appmain.GIT is not None  # only registered when git mode is on
    return appmain.GIT


async def _git_run(op: str, fn):  # type: ignore[no-untyped-def]
    return await asyncio.to_thread(_guard, op, fn)


def _token_is_rw(ctx: Context) -> bool:
    request = ctx.request_context.request
    return request is not None and getattr(request.state, "token_scope", None) == "rw"


async def git_status(ctx: Context, fetch: bool = False, limit: int = 500) -> dict[str, Any]:
    """Branch, ahead/behind, and every changed file across the WHOLE repository,
    each marked `in_scope` (inside your folder, so yours to commit) or not (a
    person's work elsewhere in the repo: you can see that it changed, but you
    cannot commit, pull over, or push it). Paths are given as `path`
    (repo-relative) and `scope_path` (relative to your folder, null if outside;
    pass it straight to read_file). In-scope changes are listed first.

    ahead/behind are only as fresh as the last fetch (see `as_of_fetch`);
    `fetch=true` runs git_fetch first, which needs the write tools and a
    read-write token. Works with a read-only token otherwise."""
    token_rw = _token_is_rw(ctx)
    return await _git_run(
        "git_status", lambda: _git().status_tool(fetch=fetch, token_rw=token_rw, limit=limit)
    )


async def git_log(ctx: Context, limit: int = 20, scope_only: bool = False) -> dict[str, Any]:
    """The last `limit` (max 200) commits on the current branch: hash, author,
    date, subject, and `touches_outside_scope` (true if the commit changed
    anything outside your folder). `scope_only=true` lists only commits that
    touched your folder. Commit bodies are not returned."""
    return await _git_run("git_log", lambda: _git().log(limit=limit, scope_only=scope_only))


async def git_diff(
    ctx: Context,
    staged: bool = False,
    path: str | None = None,
    offset: int = 0,
    length: int | None = None,
) -> dict[str, Any]:
    """The diff of YOUR FOLDER ONLY against HEAD (or, with staged=true, the
    index against HEAD). Changes elsewhere in the repository never appear here,
    even when they exist; git_status shows only their names. `path` (relative
    to your folder) narrows it to one file or subfolder. Large diffs are paged
    like read_file: pass the `offset` from the note to continue. Does not show
    untracked files; use git_status for those."""
    from . import main as appmain

    limit = appmain.CONFIG.mcp_inline_limit_chars
    return await _git_run(
        "git_diff",
        lambda: _git().diff(staged=staged, path=path, offset=offset, length=length, inline_limit=limit),
    )


async def git_commit(ctx: Context, message: str, paths: list[str] | None = None) -> dict[str, Any]:
    """Commit changes inside YOUR FOLDER. Omit `paths` to commit everything
    changed there, or pass folder-relative paths (files or subfolders) to commit
    only those. Changes anywhere else in the repository are never included, even
    if a person staged them: this builds the commit from your folder alone. The
    author is fixed by the server. New (untracked) files and folders named in
    `paths` are added. Any requested path that contributed nothing is listed in
    the result's `skipped` (with a reason) rather than being dropped silently.
    Refuses with nothing_to_commit, or
    too_many_changes if a runaway number of files changed (commit a narrower
    `paths`). Does not push; call git_push. Requires a read-write token."""
    _require_write(ctx)
    return await _git_run("git_commit", lambda: _git().commit(message, paths))


async def git_fetch(ctx: Context) -> dict[str, Any]:
    """Update the remote-tracking branch only; your files and your branch are
    not touched. Use it to see how far behind you are (`branch.behind`) before
    choosing a git_pull strategy. Requires a read-write token."""
    _require_write(ctx)
    return await _git_run("git_fetch", lambda: _git().fetch())


async def git_pull(ctx: Context, strategy: str = "ff-only") -> dict[str, Any]:
    """Fetch and integrate the remote branch. `strategy`: "ff-only" (default;
    refuses with `diverged` if you have unpushed commits the remote lacks),
    "merge" (merge commit), or "rebase" (replay your unpushed commits on top).

    Every outcome is decided BEFORE anything changes: a pull that would conflict
    is refused (`conflict`) with the tree untouched, never left half-merged. A
    pull that would change files OUTSIDE your folder is refused (`outside_scope`)
    unless the server allows pulling remote content there; the refusal lists the
    paths so you can ask the user to pull on the host. Refused while uploads are
    in flight (`uploads_in_flight`: finish or abort them first), and with
    `dirty_worktree` if uncommitted changes would be overwritten (git_commit
    yours first). Requires a read-write token."""
    _require_write(ctx)
    return await _git_run("git_pull", lambda: _git().pull(strategy))


async def git_push(ctx: Context) -> dict[str, Any]:
    """Push the current branch to the configured remote. Never forced. If the
    remote has commits you lack, refuses with `non_fast_forward`: git_pull, then
    git_push again. Refuses with `outside_scope_commits` if any commit being
    published changes files outside your folder (for example a commit a person
    made on the host): those must be pushed from the host, and the refusal names
    the commits. Requires a read-write token."""
    _require_write(ctx)
    return await _git_run("git_push", lambda: _git().push())


async def git_merge(ctx: Context, ref: str, no_ff: bool = False) -> dict[str, Any]:
    """Merge another branch into the current one. `ref` is a local branch name
    or `<remote>/<branch>` (e.g. "origin/feature"); raw SHAs, tags and revision
    syntax are refused (`invalid_ref`). Same guarantees as git_pull: a conflict
    or a change outside your folder is refused before anything is touched, and
    it is refused while uploads are in flight. `no_ff=true` always makes a merge
    commit. Requires a read-write token."""
    _require_write(ctx)
    return await _git_run("git_merge", lambda: _git().merge(ref, no_ff))


def _register_git_tools() -> None:
    from . import main as appmain

    cfg = appmain.CONFIG
    reads = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
    local_writes = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
    net_writes = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True)
    if cfg.enable_git_read:
        for fn in (git_status, git_log, git_diff):
            mcp.add_tool(fn, annotations=reads)
    if cfg.enable_git_write:
        for fn in (git_commit, git_merge):
            mcp.add_tool(fn, annotations=local_writes)
        for fn in (git_fetch, git_pull, git_push):
            mcp.add_tool(fn, annotations=net_writes)


_register_git_tools()


# =============================================================================
# Extension tools -- docs/extensions.md
#
# Last, so a core tool always owns its name: an extension that tries to reuse
# one stops the server from starting (ExtensionAPI.add_tool).
# =============================================================================


def _register_extensions() -> None:
    from . import main as appmain

    _ext.register_all(
        _EXTENSIONS,
        mcp=mcp,
        config=appmain.CONFIG,
        vault=appmain.VAULT,
        git=appmain.GIT,
        guard=_guard,
        require_write=_require_write,
    )


_register_extensions()


def extensions_info() -> dict[str, Any]:
    return _ext.info_block(_EXTENSIONS)
