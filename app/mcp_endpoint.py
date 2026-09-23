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
The one exception is `_transport_security()`, called once at module load to
build the `mcp` object below -- it does the same deferred lookup, but its
call happens synchronously during `app/main.py`'s own bottom-of-file
`from . import mcp_endpoint` statement, at which point `app.main.CONFIG` is
already set (it's assigned near the top of that file, long before the
import that pulls this module in).
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
import binascii
import contextlib
import hashlib
import hmac
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
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from .safepath import PathError

log = logging.getLogger("claude-files.mcp")

_PREVIEW_CHARS = 2000

_INSTRUCTIONS = """\
A file API scoped to one shared folder on a remote machine, exposed here as
MCP tools instead of raw REST calls.

This folder is a git repository. Anything written through these tools is
committed and pushed automatically, usually within a couple of minutes --
writes are effectively permanent and enter public git history. Never write
credentials, tokens, keys, or personal data. There is no append or partial
edit, only whole-file replace: assemble a file completely, then write it
once, rather than using the folder as scratch space.

Call get_folder_info first -- it reports this token's scope (rw/ro), the
server's limits, and the inline size budgets read_file/write_file operate
under. A file too large or too binary for a tool result is refused with an
explanation naming the equivalent REST endpoint, never silently truncated
or mangled.
"""


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


mcp = FastMCP(
    "claude-files",
    instructions=_INSTRUCTIONS,
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
    except ValueError as exc:
        appmain.log.warning("denied MCP %s -> %s", op, exc)
        raise


def _require_write(ctx: Context) -> None:
    request = ctx.request_context.request
    if request is None or getattr(request.state, "token_scope", None) != "rw":
        # Same text as require_write's HTTPException (app/main.py) -- one
        # string to grep for, not two.
        raise ValueError("This token is read-only")


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


# =============================================================================
# Tools -- one per REST capability (docs/plan-mcp-connector.md, "Tool
# surface"). find_files is deliberately not included yet: the plan's open
# question 5 defers it until list_files(recursive=True) proves clumsy in
# real use, since removing a tool from a published connector is harder than
# adding one later.
# =============================================================================


@mcp.tool()
def get_folder_info(ctx: Context) -> dict[str, Any]:
    """Server limits, policy, and this token's scope (rw/ro). Call this
    first -- it also reports the inline size budgets read_file and
    write_file operate under, and the chunk size begin_write/write_chunk
    advise."""
    from . import main as appmain

    request = ctx.request_context.request
    scope = getattr(request.state, "token_scope", "ro") if request is not None else "ro"
    return {
        "max_upload_bytes": appmain.CONFIG.max_upload_bytes,
        "max_list_entries": appmain.CONFIG.max_list_entries,
        "denied_names": sorted(appmain.CONFIG.denied_names),
        "allow_delete": appmain.CONFIG.allow_delete,
        "allow_hardlinked_files": appmain.CONFIG.allow_hardlinked_files,
        "token_scope": scope,
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
        "bridge": {
            "available": appmain._bridge_script_path() is not None,
            "download_url": "/v1/bridge.py",
            "note": (
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


@mcp.tool()
def read_file(
    path: str, encoding: str = "utf-8", max_chars: int | None = None
) -> dict[str, Any]:
    """Read a file's contents. `encoding="utf-8"` (default) decodes the file
    as text; a file containing a NUL byte or that fails UTF-8 decoding is
    refused with an explanation, never returned as mojibake or silently
    base64'd. Pass `encoding="base64"` for a small binary file (capped well
    below the text budget -- see get_folder_info). A result over the inline
    character budget (`max_chars`, or the server default) comes back as
    {preview, bytes, truncated: true, note} naming the REST endpoint for the
    full content -- never a silent cut. Every successful result -- base64,
    full text, or a truncated preview -- includes `bytes` and `sha256` for
    the whole file, so a copy can be verified by forwarding them straight
    into write_file's `expect_bytes`/`expect_sha256` without computing
    anything yourself."""
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

        if encoding == "base64":
            cap = appmain.CONFIG.mcp_base64_limit_bytes
            if len(data) > cap:
                raise ValueError(
                    f"'{rel}' is {len(data):,} bytes, over the {cap:,}-byte base64 "
                    f"limit for this connector. Fetch it directly with "
                    f"GET /v1/content/{rel}."
                )
            return {
                "content": base64.b64encode(data).decode("ascii"),
                "encoding": "base64",
                "bytes": len(data),
                "sha256": digest,
            }

        if encoding != "utf-8":
            raise ValueError(f"Unknown encoding {encoding!r}; expected 'utf-8' or 'base64'")

        if b"\x00" in data:
            raise ValueError(
                f"'{rel}' contains a NUL byte and cannot be decoded as UTF-8 text "
                f"({len(data):,} bytes). Use encoding=\"base64\" if it fits that "
                f"budget, or GET /v1/content/{rel} directly."
            )
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            raise ValueError(
                f"'{rel}' is not valid UTF-8 and cannot be read as text "
                f"({len(data):,} bytes). Use encoding=\"base64\" if it fits that "
                f"budget, or GET /v1/content/{rel} directly."
            ) from None

        limit = appmain.CONFIG.mcp_inline_limit_chars
        if max_chars is not None:
            limit = min(limit, max_chars)
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
                f"fetch the full file with GET /v1/content/{rel}."
            ),
        }

    return _guard("read_file", _do)


@mcp.tool()
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
    """Create or replace a file. `parents` defaults to true here (unlike the
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
    Requires a read-write token. Remember: this folder is a git repository
    and writes are committed and pushed automatically -- never write
    credentials or secrets."""
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
                    f"single-shot base64 limit for this connector. If you can run "
                    f"shell commands or a local MCP server, fetch /v1/bridge.py with "
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
        return {"ok": True, "path": "/".join(parts), "size": len(data), "created": True}

    return _guard("write_file", _do)


@mcp.tool()
def begin_write(
    path: str,
    ctx: Context,
    encoding: str = "utf-8",
    overwrite: bool = True,
    parents: bool = True,
) -> dict[str, Any]:
    """Start a chunked write session for a file too large for write_file's
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
def delete_file(path: str, ctx: Context, recursive: bool = False) -> dict[str, Any]:
    """Delete a file, or a directory (with recursive=true for a non-empty
    one). Honours ALLOW_DELETE -- refused entirely if the server disables
    deletion. Deleting does NOT remove the file from git history: the
    shared folder is auto-committed and pushed, so a prior version remains
    reachable in `git log` even after this call. Requires a read-write
    token."""
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
