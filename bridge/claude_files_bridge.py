#!/usr/bin/env python3
"""Local upload/download bridge for claude-files.

Runs in the AGENT's environment (a cloud workspace, a laptop, whatever has
the local file) and streams bytes directly to/from
`PUT`/`GET /v1/content/{path}` on a claude-files server -- the same REST API
`app/main.py` serves, over plain HTTPS. It exists because the connector's
`write_file`/`read_file` MCP tools require content to pass through the
calling model's own generation as base64, and that ceiling (whatever a model
can emit in one tool call, observed under 10 KB) is far smaller than what a
real file needs. See docs/feature-request-path-based-upload.md for the full
design brief -- read that before changing this file's behaviour.

Three ways to run this, one implementation underneath:

  stdio MCP server   python3 claude_files_bridge.py serve
  CLI                python3 claude_files_bridge.py upload ./brief.pdf briefs/2026/brief.pdf
  import              from claude_files_bridge import upload, download, check

Stdlib-only at import time: `import mcp` happens inside `_serve()`, never at
module top level, so `upload`/`download`/`check` work on a bare `python3`
with nothing installed -- that is what makes the CLI mode a one-curl fix on
an unknown surface. `mcp` (pinned in bridge/requirements.txt) is needed only
for `serve`.

Targets Python 3.9+ deliberately -- this lands on machines we do not
control, unlike the app itself (3.12). Deliberately NOT `from __future__
import annotations`, unlike every other module in this repo -- see
app/mcp_endpoint.py's module docstring, gotcha reproduced here unchanged:
mcp==1.12.4's `Tool.from_function()` (tools/base.py) reads
`inspect.signature(fn)` without `eval_str=True` and then calls
`issubclass(param.annotation, Context)` on whatever it gets. With PEP 563
active, every annotation in a tool function arrives as a plain string, and
`issubclass("dict", Context)` raises `TypeError: issubclass() arg 1 must be
a class` for literally any parameter -- reproduced directly against this
pin, and it takes `_serve()` down at tool-registration time before a single
request is served. `dict[str, Any]`, `tuple[...]`, and `Optional[X]` (never
`X | Y`) are used below instead, which are all safe to evaluate eagerly on
3.9+ without the future import -- PEP 585 generic subscripting of builtin
containers landed in 3.9 itself, independent of PEP 563.

The token is never a tool argument, never a CLI argument, and never printed:
it is configured through the environment only (see CONFIG below) and sent
solely as an `Authorization: Bearer` header. In `serve` mode, stdout is the
MCP transport -- every diagnostic goes to `stderr` via `logging`, and this
module never calls `print()` outside the CLI's own human/JSON output path.
"""

import argparse
import hashlib
import json
import logging
import os
import ssl
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Optional

log = logging.getLogger("claude-files-bridge")
if not log.handlers:
    _handler = logging.StreamHandler(sys.stderr)
    _handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    log.addHandler(_handler)
    log.setLevel(logging.INFO)
    log.propagate = False

USER_AGENT = "claude-files-bridge/1.1.0"

_UPLOAD_HASH_BLOCK = 1024 * 1024        # 1 MiB -- hashing block size
_DOWNLOAD_BLOCK = 256 * 1024            # 256 KiB -- download streaming block size
_RETRY_DELAYS = (2, 4, 8)               # seconds; at most 3 retries
_INFO_CACHE_TTL_SECONDS = 300           # re-fetch GET /v1/info if older than this


# ==========================================================================
# Errors. Each maps to a CLI exit code (bridge/README.md has the table).
# ==========================================================================

class BridgeError(Exception):
    """Base for everything this module raises deliberately."""
    exit_code = 1


class UsageError(BridgeError):
    """Missing/invalid configuration, or a bad argument (e.g. a hostile
    remote_path). Exit code 1."""
    exit_code = 1


class LocalFileError(BridgeError):
    """A problem with the local file: missing, a directory, unreadable, or
    over the server's max_upload_bytes. Exit code 2."""
    exit_code = 2


class ServerRefusal(BridgeError):
    """The server answered with a 4xx. Exit code 3."""
    exit_code = 3

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(f"HTTP {status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message


class TransportError(BridgeError):
    """Connection/DNS/TLS failure after retries, or a persistent 5xx. Exit
    code 4."""
    exit_code = 4


class IntegrityError(BridgeError):
    """The server accepted the upload (2xx) but the sha256 it reports does
    not match what was sent. Exit code 5."""
    exit_code = 5


# ==========================================================================
# Configuration -- environment only, never a tool/CLI argument.
# ==========================================================================

def _get_base_url() -> str:
    url = os.environ.get("CLAUDE_FILES_URL", "").strip()
    if not url:
        raise UsageError(
            "CLAUDE_FILES_URL is not set. See bridge/README.md for setup."
        )
    return url.rstrip("/")


def _get_token() -> tuple[str, str]:
    """Returns (token, the env var it came from) -- never the token's value
    in a log line or exception message."""
    token_file = os.environ.get("CLAUDE_FILES_TOKEN_FILE", "").strip()
    if token_file:
        try:
            with open(token_file, "r", encoding="utf-8") as f:
                token = f.read().strip()
        except OSError as exc:
            raise UsageError(
                f"CLAUDE_FILES_TOKEN_FILE={token_file!r} could not be read: {exc}. "
                f"See bridge/README.md for setup."
            ) from None
        if not token:
            raise UsageError(
                f"CLAUDE_FILES_TOKEN_FILE={token_file!r} is empty. "
                f"See bridge/README.md for setup."
            )
        return token, "CLAUDE_FILES_TOKEN_FILE"

    token = os.environ.get("CLAUDE_FILES_TOKEN", "").strip()
    if token:
        return token, "CLAUDE_FILES_TOKEN"

    raise UsageError(
        "Neither CLAUDE_FILES_TOKEN nor CLAUDE_FILES_TOKEN_FILE is set. "
        "See bridge/README.md for setup."
    )


def _get_timeout() -> float:
    raw = os.environ.get("CLAUDE_FILES_TIMEOUT", "").strip()
    if not raw:
        return 60.0
    try:
        return float(raw)
    except ValueError:
        raise UsageError(f"CLAUDE_FILES_TIMEOUT={raw!r} is not a number") from None


def _ca_bundle_path() -> Optional[str]:
    """First of SSL_CERT_FILE / REQUESTS_CA_BUNDLE that names a readable
    file, in that order -- or None for the system default."""
    for var in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"):
        path = os.environ.get(var, "").strip()
        if path and os.path.isfile(path):
            return path
    return None


def _ssl_context() -> ssl.SSLContext:
    """No flag disables verification, deliberately -- see this module's
    docstring and docs/feature-request-path-based-upload.md."""
    ctx = ssl.create_default_context()
    bundle = _ca_bundle_path()
    if bundle:
        ctx.load_verify_locations(bundle)
    return ctx


# ==========================================================================
# Remote path handling
# ==========================================================================

def _quote_remote_path(remote_path: str) -> str:
    """Validate and percent-quote a path relative to the shared folder root.

    This is NOT the security boundary -- the server's own app/safepath.py
    is, and it re-validates independently. This exists purely so a caller
    gets a legible local error instead of a round trip for the obviously
    hostile cases (a '.'/'..' segment, a NUL byte). Deliberately does not
    duplicate any more of the server's containment policy than that.
    """
    segments = [s for s in remote_path.split("/") if s != ""]
    if not segments:
        raise UsageError(f"remote_path {remote_path!r} is empty")
    for seg in segments:
        if "\x00" in seg:
            raise UsageError(f"remote_path {remote_path!r} contains a NUL byte")
        if seg in (".", ".."):
            raise UsageError(
                f"remote_path {remote_path!r} contains a {seg!r} segment, which the "
                f"server will refuse -- fix the path rather than retrying."
            )
    return "/".join(urllib.parse.quote(seg, safe="") for seg in segments)


# ==========================================================================
# HTTP helpers
# ==========================================================================

def _parse_json_body(body: bytes) -> dict:
    try:
        parsed = json.loads(body.decode("utf-8"))
        if isinstance(parsed, dict):
            return parsed
    except (ValueError, UnicodeDecodeError):
        pass
    return {"code": "error", "message": body[:500].decode("utf-8", "replace")}


_INFO_CACHE: dict[str, Any] = {"data": None, "at": 0.0}


def _fetch_info(base_url: str, token: str, timeout: float, ctx: ssl.SSLContext) -> dict:
    """GET /v1/info, cached for the process lifetime (re-fetched if older
    than _INFO_CACHE_TTL_SECONDS, which mostly matters in `serve` mode where
    the process lives a long time)."""
    now = time.time()
    cached = _INFO_CACHE.get("data")
    if cached is not None and now - _INFO_CACHE["at"] < _INFO_CACHE_TTL_SECONDS:
        return cached
    req = urllib.request.Request(
        f"{base_url}/v1/info",
        headers={"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            info = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        parsed = _parse_json_body(exc.read())
        raise ServerRefusal(exc.code, parsed.get("code", "error"), parsed.get("message", "")) from None
    except (urllib.error.URLError, OSError) as exc:
        raise TransportError(f"Could not reach {base_url}/v1/info: {exc}") from exc
    _INFO_CACHE["data"] = info
    _INFO_CACHE["at"] = now
    return info


def _stream_request(
    method: str,
    url: str,
    *,
    local_path: str,
    extra_headers: dict[str, str],
    token: str,
    timeout: float,
    ctx: ssl.SSLContext,
) -> tuple[int, dict, bytes, int]:
    """PUT/POST the file's bytes, streamed (never `f.read()`d whole).

    Returns (status, response headers, body, retries_performed). Retries
    only transport failures (URLError/OSError) or HTTP 502/503/504, at most
    3 times with 2s/4s/8s backoff -- a 4xx is an answer, never retried. The
    file is reopened on each attempt, which is the retry-safe equivalent of
    seek(0).
    """
    attempts = 0
    while True:
        try:
            with open(local_path, "rb") as f:
                content_length = os.fstat(f.fileno()).st_size
                req = urllib.request.Request(url, method=method, data=f)
                req.add_header("Authorization", f"Bearer {token}")
                req.add_header("Content-Type", "application/octet-stream")
                req.add_header("Content-Length", str(content_length))
                req.add_header("User-Agent", USER_AGENT)
                for key, value in extra_headers.items():
                    req.add_header(key, value)
                try:
                    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                        return resp.status, dict(resp.headers), resp.read(), attempts
                except urllib.error.HTTPError as exc:
                    body = exc.read()
                    if exc.code in (502, 503, 504) and attempts < len(_RETRY_DELAYS):
                        log.warning(
                            "%s %s -- retrying in %ds (attempt %d)",
                            exc.code, url, _RETRY_DELAYS[attempts], attempts + 1,
                        )
                        time.sleep(_RETRY_DELAYS[attempts])
                        attempts += 1
                        continue
                    return exc.code, dict(exc.headers or {}), body, attempts
        except (urllib.error.URLError, OSError) as exc:
            if attempts < len(_RETRY_DELAYS):
                log.warning(
                    "transport error contacting %s: %s -- retrying in %ds (attempt %d)",
                    url, exc, _RETRY_DELAYS[attempts], attempts + 1,
                )
                time.sleep(_RETRY_DELAYS[attempts])
                attempts += 1
                continue
            raise TransportError(
                f"Transport failure contacting {url} after {attempts + 1} attempt(s): {exc}"
            ) from exc


# ==========================================================================
# The upload core
# ==========================================================================

def upload(
    local_path: str,
    remote_path: str,
    *,
    overwrite: bool = True,
    parents: bool = True,
) -> dict:
    """Stream a local file into the shared folder. Returns a result dict.

    Never reads the whole file into memory -- hashed and uploaded in fixed-
    size blocks regardless of file size. Verifies the server's reported
    sha256 against the local digest before calling the upload a success;
    see IntegrityError.
    """
    base_url = _get_base_url()
    token, _source = _get_token()
    timeout = _get_timeout()
    ctx = _ssl_context()

    try:
        st = os.stat(local_path)
    except FileNotFoundError:
        raise LocalFileError(f"{local_path!r} does not exist") from None
    except OSError as exc:
        raise LocalFileError(f"{local_path!r} could not be stat'd: {exc}") from None
    if stat.S_ISDIR(st.st_mode):
        raise LocalFileError(f"{local_path!r} is a directory; upload one file, or tar it first")
    if not stat.S_ISREG(st.st_mode):
        raise LocalFileError(f"{local_path!r} is not a regular file")

    size = st.st_size
    digest = hashlib.sha256()
    try:
        with open(local_path, "rb") as f:
            while True:
                block = f.read(_UPLOAD_HASH_BLOCK)
                if not block:
                    break
                digest.update(block)
    except OSError as exc:
        raise LocalFileError(f"{local_path!r} could not be read: {exc}") from None
    sha256_hex = digest.hexdigest()

    # Validate remote_path before any network call -- a hostile or malformed
    # path should not cost a round trip to discover.
    quoted = _quote_remote_path(remote_path)

    info = _fetch_info(base_url, token, timeout, ctx)
    max_upload_bytes = info.get("max_upload_bytes")
    if isinstance(max_upload_bytes, int) and size > max_upload_bytes:
        raise LocalFileError(
            f"{local_path!r} is {size:,} bytes, over this server's max_upload_bytes "
            f"({max_upload_bytes:,}). Refusing to stream it -- it would only earn a 413."
        )

    url = f"{base_url}/v1/content/{quoted}"
    if parents:
        url += "?parents=true"
    method = "PUT" if overwrite else "POST"

    status, _headers, body, attempts = _stream_request(
        method, url,
        local_path=local_path,
        extra_headers={"X-Expect-Bytes": str(size), "X-Expect-Sha256": sha256_hex},
        token=token, timeout=timeout, ctx=ctx,
    )

    if status >= 500:
        parsed = _parse_json_body(body)
        raise TransportError(
            f"Server returned {status} after {attempts} retr"
            f"{'y' if attempts == 1 else 'ies'} contacting {url}: "
            f"{parsed.get('message') or body[:200]!r}"
        )

    parsed = _parse_json_body(body)
    if status >= 400:
        if method == "POST" and status == 409 and attempts > 0:
            raise ServerRefusal(
                status, parsed.get("code", "exists"),
                "the server says this path now exists; the first attempt may have "
                "succeeded -- stat it and compare the size before retrying. "
                + parsed.get("message", ""),
            )
        raise ServerRefusal(status, parsed.get("code", "error"), parsed.get("message", ""))

    server_sha = parsed.get("sha256")
    verified = True
    note = None
    if server_sha is None:
        verified = False
        note = (
            "The server did not return a sha256 -- it is likely pre-1.1.0 (see "
            "docs/feature-request-path-based-upload.md, Part 0) and this upload "
            "could not be confirmed end to end."
        )
    elif server_sha.lower() != sha256_hex.lower():
        raise IntegrityError(
            f"Uploaded {local_path!r} but the server reports sha256 {server_sha}, which "
            f"does not match the local digest {sha256_hex}. Do not trust {remote_path!r}."
        )

    result = {
        "ok": True,
        "remote_path": parsed.get("path", remote_path.strip("/")),
        "size": size,
        "sha256": sha256_hex,
        "verified": verified,
        "created": bool(parsed.get("created", True)),
        "url": url.split("?", 1)[0],
    }
    if note:
        result["note"] = note
    return result


# ==========================================================================
# The download core
# ==========================================================================

def download(remote_path: str, local_path: str, *, overwrite: bool = False) -> dict:
    """Fetch a file from the shared folder onto local disk, streamed in
    fixed-size blocks to `local_path + ".part"`, then `os.replace()`d into
    place -- the same write-temp-then-rename discipline the server uses, so
    a killed download never leaves a half-file wearing the real name."""
    base_url = _get_base_url()
    token, _source = _get_token()
    timeout = _get_timeout()
    ctx = _ssl_context()

    if os.path.exists(local_path) and not overwrite:
        raise LocalFileError(
            f"{local_path!r} already exists; pass overwrite=True to replace it"
        )

    quoted = _quote_remote_path(remote_path)
    url = f"{base_url}/v1/content/{quoted}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "User-Agent": USER_AGENT,
    })

    tmp_path = local_path + ".part"
    digest = hashlib.sha256()
    size = 0
    try:
        try:
            resp = urllib.request.urlopen(req, timeout=timeout, context=ctx)
        except urllib.error.HTTPError as exc:
            parsed = _parse_json_body(exc.read())
            raise ServerRefusal(exc.code, parsed.get("code", "error"), parsed.get("message", "")) from None
        except (urllib.error.URLError, OSError) as exc:
            raise TransportError(f"Could not reach {url}: {exc}") from exc

        with resp:
            with open(tmp_path, "wb") as out:
                while True:
                    block = resp.read(_DOWNLOAD_BLOCK)
                    if not block:
                        break
                    digest.update(block)
                    size += len(block)
                    out.write(block)
        os.replace(tmp_path, local_path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    return {
        "ok": True,
        "remote_path": remote_path.strip("/"),
        "local_path": local_path,
        "size": size,
        "sha256": digest.hexdigest(),
        "verified": True,
        "created": True,
    }


# ==========================================================================
# check() -- diagnose a broken setup in one call
# ==========================================================================

def check() -> dict:
    """Report the resolved base URL, whether a token was found and from
    which variable (never its value), GET /health's status and version,
    GET /v1/info's limits, the proxy in use, and the CA bundle in use if not
    the default. Every failure mode gets a one-line `remedy` in the result."""
    result: dict[str, Any] = {}

    base_url: Optional[str]
    try:
        base_url = _get_base_url()
        result["base_url"] = base_url
    except UsageError as exc:
        base_url = None
        result["base_url"] = None
        result["remedy"] = str(exc)

    token: Optional[str]
    try:
        token, source = _get_token()
        result["token_found"] = True
        result["token_source"] = source
    except UsageError as exc:
        token = None
        result["token_found"] = False
        if "remedy" not in result:
            result["remedy"] = str(exc)

    result["proxy"] = urllib.request.getproxies()
    bundle = _ca_bundle_path()
    result["ca_bundle"] = bundle or "system default"

    if base_url is None:
        return result

    timeout = _get_timeout()
    ctx = _ssl_context()

    try:
        req = urllib.request.Request(f"{base_url}/health", headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            health = json.loads(resp.read())
        result["health"] = health
    except Exception as exc:
        result["health_error"] = str(exc)
        result["remedy"] = (
            f"GET {base_url}/health failed: {exc}. Check CLAUDE_FILES_URL and network "
            f"reachability (proxy: {result['proxy']})."
        )
        return result

    if token is None:
        return result

    try:
        req = urllib.request.Request(
            f"{base_url}/v1/info",
            headers={"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT},
        )
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            info = json.loads(resp.read())
        result["max_upload_bytes"] = info.get("max_upload_bytes")
        result["mcp_enabled"] = info.get("mcp_enabled")
        result["bridge_available"] = info.get("bridge_available")
        result["note"] = (
            "Scope (rw/ro) is not probed destructively. If an upload fails with "
            "403 readonly, this token is API_TOKEN_READONLY."
        )
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            result["remedy"] = "401 from GET /v1/info -- the token is wrong."
        else:
            result["remedy"] = f"GET /v1/info returned {exc.code}."
    except Exception as exc:
        result["remedy"] = f"Could not reach {base_url}/v1/info: {exc}"

    return result


# ==========================================================================
# serve() -- stdio MCP server. `mcp` is imported only here.
# ==========================================================================

def _serve() -> int:
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError:
        print(
            "The 'mcp' package is required for `serve` mode: "
            "pip install -r bridge/requirements.txt",
            file=sys.stderr,
        )
        return 1

    mcp_server = FastMCP("claude-files-bridge")

    @mcp_server.tool()
    def upload_file(
        local_path: str, remote_path: str, overwrite: bool = True, parents: bool = True
    ) -> dict:
        """Upload a local file into the shared claude-files folder by path.
        The bytes are read from disk and streamed directly -- they never
        pass through your output, so there is no size ceiling from your
        side. Use this for any binary file (PDF, image, archive) and for
        any text file over a few KB, instead of the connector's
        `write_file`, which requires you to type the content out.
        `remote_path` is relative to the shared folder root. Returns the
        size and a sha256 verified by the server. Remember: that folder is
        a git repository and writes are committed and pushed automatically
        -- never upload credentials or secrets."""
        return upload(local_path, remote_path, overwrite=overwrite, parents=parents)

    @mcp_server.tool()
    def download_file(remote_path: str, local_path: str, overwrite: bool = False) -> dict:
        """Fetch a file from the shared folder onto local disk by path,
        without its bytes passing through your context. Use this instead of
        `read_file(encoding='base64')` for anything binary or large."""
        return download(remote_path, local_path, overwrite=overwrite)

    @mcp_server.tool()
    def bridge_status() -> dict:
        """Check that the bridge can reach the server: base URL, token
        presence and scope, server version, size limits. Call this first if
        an upload fails."""
        return check()

    mcp_server.run()
    return 0


# ==========================================================================
# CLI
# ==========================================================================

def _run_and_report(fn: Callable[[], dict], as_json: bool) -> int:
    try:
        result = fn()
    except BridgeError as exc:
        if as_json:
            print(json.dumps({"ok": False, "error": str(exc), "exit_code": exc.exit_code}))
        else:
            print(f"error: {exc}")
        return exc.exit_code
    if as_json:
        print(json.dumps(result))
    else:
        for key, value in result.items():
            print(f"{key}: {value}")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="claude_files_bridge.py",
        description=__doc__.splitlines()[0] if __doc__ else "",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Emit a single JSON object instead of prose, for a calling agent to parse",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # --json is accepted both before and after the subcommand, since either
    # order reads naturally (`upload --json a b` and `--json upload a b`
    # both work) -- defined on the top-level parser AND each subparser,
    # sharing the same dest.
    p_upload = sub.add_parser("upload", help="Upload a local file")
    p_upload.add_argument("local_path")
    p_upload.add_argument("remote_path")
    p_upload.add_argument("--no-overwrite", action="store_true", help="Fail (409) if remote_path already exists")
    p_upload.add_argument("--no-parents", action="store_true", help="Do not create missing parent directories")
    p_upload.add_argument("--json", action="store_true", help=argparse.SUPPRESS)

    p_download = sub.add_parser("download", help="Download a remote file")
    p_download.add_argument("remote_path")
    p_download.add_argument("local_path")
    p_download.add_argument("--overwrite", action="store_true", help="Replace local_path if it already exists")
    p_download.add_argument("--json", action="store_true", help=argparse.SUPPRESS)

    p_check = sub.add_parser("check", help="Diagnose the configured connection")
    p_check.add_argument("--json", action="store_true", help=argparse.SUPPRESS)

    sub.add_parser("serve", help="Run as a stdio MCP server (requires the mcp package)")

    return parser


def main(argv: Optional[list] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "serve":
        return _serve()
    if args.command == "upload":
        return _run_and_report(
            lambda: upload(
                args.local_path, args.remote_path,
                overwrite=not args.no_overwrite, parents=not args.no_parents,
            ),
            args.json,
        )
    if args.command == "download":
        return _run_and_report(
            lambda: download(args.remote_path, args.local_path, overwrite=args.overwrite),
            args.json,
        )
    if args.command == "check":
        return _run_and_report(check, args.json)

    parser.error(f"unknown command {args.command!r}")
    return 1  # pragma: no cover -- argparse.error() exits the process


if __name__ == "__main__":
    sys.exit(main())
