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
import logging
import os
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
    """Run a Vault operation, logging (and translating) a PathError.

    FastMCP catches an exception raised inside a tool and turns it into a
    JSON-RPC result with isError: true and HTTP 200 -- it never reaches
    app/main.py's `_path_error_handler`, so a rejected path would otherwise
    be invisible in the logs, silently weakening the observability half of
    the security model. Every tool routes its Vault calls through here so
    that can't be forgotten per-tool.
    """
    from . import main as appmain

    try:
        return fn(*args, **kwargs)
    except PathError as exc:
        appmain.log.warning("denied MCP %s -> %s (%s)", op, exc.code, exc.message)
        raise ValueError(f"{exc.code}: {exc.message}") from exc


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
    write_file operate under."""
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
    full content -- never a silent cut."""
    from . import main as appmain

    def _do():  # type: ignore[no-untyped-def]
        parts = appmain.VAULT.components(path)
        rel = "/".join(parts)
        fd, _st = appmain.VAULT.open_read(parts)
        try:
            data = _read_all(fd)
        finally:
            os.close(fd)

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
            return {"text": text}

        return {
            "preview": text[:_PREVIEW_CHARS],
            "bytes": len(data),
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
) -> dict[str, Any]:
    """Create or replace a file. `parents` defaults to true here (unlike the
    REST API): a model that has just been told a path doesn't exist isn't
    helped by a 409, so the missing parent chain is created automatically.
    `overwrite=false` refuses if the file already exists. `encoding="utf-8"`
    (default) treats `content` as text; `encoding="base64"` decodes it as
    binary, capped well below the text budget (see get_folder_info).
    Writes are atomic -- a reader never sees a half-written file. Requires a
    read-write token. Remember: this folder is a git repository and writes
    are committed and pushed automatically -- never write credentials or
    secrets."""
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
                    f"base64 limit for this connector. Use PUT /v1/content/{{path}} "
                    f"over REST for larger files."
                )
        else:
            raise ValueError(f"Unknown encoding {encoding!r}; expected 'utf-8' or 'base64'")

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
