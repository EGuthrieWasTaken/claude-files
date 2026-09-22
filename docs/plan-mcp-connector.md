# Feature request: an MCP connector surface for claude-files

**Status: proposed. Nothing here is implemented.** Every "verified" note below
means it was reproduced in a scratch environment against the real `mcp` SDK
**on this repo's own pinned versions**, not that the code exists in this
repository. The things marked *unverified* are the ones an implementer has to
find out empirically, and they are called out as such.

The reference implementation for all of this is the sibling repo
`meeting-transcriber`, which already ships a working remote MCP endpoint:
`app/mcp_endpoint.py`, its design brief `docs/plan-phase4-remote-mcp.md`, and
its tests `tests/test_mcp_remote.py`. Read that brief before starting — it
paid for four gotchas in production that this document inherits for free, and
three of them reproduce identically here.

---

## Goal

Mount a Streamable HTTP MCP endpoint at `/mcp` on the existing FastAPI app
(`app/main.py`), so this service — already behind a Cloudflare tunnel at
`claude-files.guthrieec.dev` — can be added **once** as a custom connector in
Cowork / claude.ai and used from every surface, with the shared folder
appearing as a set of tools rather than as a REST API an agent has to be
taught.

### What changes for the user

Today, giving an agent write access to the folder means: paste `AGENT.md` into
the context, hand over `$CLAUDE_FILES_TOKEN`, and hope the agent composes
correct `curl`/`fetch` calls — including remembering `?parents=true`, the
`409 no_parent` retry, and that `PUT` replaces while `POST` refuses.

| | REST + `AGENT.md` (today) | `/mcp` connector (this proposal) |
|---|---|---|
| Discovery | agent reads a pasted brief | tools appear in the tool list, typed and described |
| Token | lives in the conversation or the environment | lives in connector settings, never in context |
| Works from Cowork / mobile / claude.ai web | only if the surface can make arbitrary HTTP calls | **yes**, natively |
| Per-machine install | none, but per-conversation setup | **none at all** |
| Large / binary files | fine — raw body, 256 MiB | **no** — see "The two hard limitations" |
| Correct-by-construction paths | agent must get it right | tool schema + server-side validation |

The REST API does not go away and is not deprecated. `/mcp` is a second front
end onto the same `Vault`, exactly as `meeting-transcriber` kept its stdio
server after mounting `/mcp`. Anything too big or too binary for a tool call
still goes over REST, and the tool errors say so explicitly.

---

## The two hard limitations

`meeting-transcriber`'s Phase 4 had one ("a remote MCP server cannot take a
file off your laptop"). A file API has two, and both are more central here
because moving file bytes *is* the product.

### 1. Tool I/O is JSON. Bulk transfer is not available.

A tool result is a JSON string in a model's context window. `MAX_UPLOAD_BYTES`
is 256 MiB (`app/config.py:38`). Those two facts do not meet. No design gets
around it — the same constraint that forced `meeting-transcriber` to keep a
stdio server for local uploads.

**Consequence:** `/mcp` gets its own inline budget, well below
`MAX_UPLOAD_BYTES`, and every over-budget result is an *explicit, explanatory*
truncation that names the REST URL for the full bytes — never a silent cut.
Follow the shape `meeting-transcriber`'s `get_transcript` already uses
(`app/mcp_endpoint.py`, the `{"preview", "bytes", "truncated", "note"}`
return).

Proposed setting: `MCP_INLINE_LIMIT_CHARS`, default **100 000** characters,
matching the sibling repo's `TRANSCRIBE_MCP_INLINE_LIMIT_CHARS`. Add it to
`Config` (`app/config.py:33`) via the existing `_env_int` helper.

### 2. Binary files are worse than large ones.

A transcript is text. This folder holds whatever an agent puts in it. Base64
inflates by 33% *and* is unreadable to the model, so a base64 payload spends
context to buy nothing.

**Proposed policy**, to be settled in review before coding:

- `read_file` decodes UTF-8. A file containing a NUL byte or failing UTF-8
  decoding is **refused** with a message naming `GET /v1/content/{path}` and
  the file's size — not returned as mojibake, and not silently base64'd.
- An explicit `encoding="base64"` argument opts in, capped far lower than the
  text budget (proposed **1 MiB** of source bytes, `MCP_BASE64_LIMIT_BYTES`).
  This exists for the real case of a small PNG or PDF an agent genuinely needs
  to hand over, and the cap is what keeps it from becoming the default path.
- `write_file` takes `encoding: "utf-8" | "base64"` under the same two caps.

---

## Decision gate 1 — which auth header the connector can actually send

**Settle this first.** It is the only thing that can make the feature
impossible, and it is cheap to check.

`_check_token()` (`app/main.py:127`) accepts exactly one credential shape:

```python
if not authorization or not authorization.startswith("Bearer "):
```

`meeting-transcriber`'s Phase 4 verified live (2026-09-22) that claude.ai's
add-custom-connector dialog tries OAuth discovery, fails, then offers **"No
sign-in"** plus up to four static request headers — and that **`x-api-key` is
one of its preset header-name options**. What that write-up does *not*
establish is whether `Authorization` is accepted as a custom header name.
Connector UIs commonly reserve `Authorization` for their own OAuth flow, and
`meeting-transcriber` never had to find out because `app/auth.py` there
accepts `X-API-Key` already.

**Recommendation: don't find out the hard way — accept both.** Widen
`_check_token()` to read the token from `X-API-Key` when `Authorization` is
absent. It is a handful of lines, it costs nothing in the REST path, and it
removes the only real chance of this feature dead-ending at the dialog. Keep
`Authorization: Bearer` as the documented primary so `AGENT.md` and every
existing `curl` in `README.md` stay correct.

Two settings-level items that come with it:

- **Cloudflare Access.** If a policy is enforced on the hostname, it rejects
  the connector at the edge with an HTML login page before the app ever sees
  the request. Add a **Bypass policy scoped to `/mcp` only** — the app's own
  token check is then the gate. Do not disable Access on the whole hostname.
- **Blast radius.** The connector's stored token is full read/write in the
  folder, reachable from anywhere. `API_TOKEN_READONLY` (`app/config.py:37`)
  exists precisely for this: a connector configured for reading should be
  given the read-only token, and the write tools must refuse it *audibly*
  (see "Token scope inside a tool" below).

**Unverified, and the implementer's step 0:** stand the endpoint up, add it as
a custom connector in Cowork, and record in this file which header names the
dialog actually offers.

---

## Decision gate 2 — the dependency cost, measured

This repo's whole pitch is "small enough to audit in one sitting":
`app/requirements.txt` is three pinned lines. `mcp` is not three lines.

`mcp` 1.30.0 requires: `anyio`, `httpx`, `httpx-sse`, `jsonschema`, `pydantic`,
`pydantic-settings`, `pyjwt`, `python-multipart`, `sse-starlette`, `starlette`,
`typing-extensions`, `typing-inspection`, `uvicorn`.

**Verified, and this is the finding that matters:** installing `mcp` on top of
the current pins *breaks them*.

```
$ pip install fastapi==0.115.6 "uvicorn[standard]==0.34.0" pydantic==2.10.4
$ pip install "mcp>=1.0,<2.0"
ERROR: pip's dependency resolver does not currently take into account all the
packages that are installed. ... fastapi 0.115.6 requires
starlette<0.42.0,>=0.40.0, but you have starlette 1.6.0 which is incompatible.
```

`mcp` 1.30.0 pulls `starlette` 1.6.0 and `pydantic` 2.13.5, silently
overwriting a pin `fastapi==0.115.6` declares it cannot work with. A separate
`requirements-mcp.txt` installed as its own Docker layer — the sibling repo's
pattern — does **not** save you here, because unlike that repo's ML pins,
`mcp` and `fastapi` genuinely share `starlette`.

Three resolutions were measured. All three are clean installs; pick one in
review:

| | `fastapi` | `pydantic` | `mcp` | `starlette` | Verdict |
|---|---|---|---|---|---|
| **A. Hold every current pin** | 0.115.6 *(unchanged)* | 2.10.4 *(unchanged)* | **1.12.4** | 0.41.3 | Resolver backtracks `mcp` to fit. No existing pin moves. |
| **B. Hold `pydantic`, bump `fastapi`** | 0.141.1 | 2.10.4 *(unchanged)* | **1.12.4** | 1.6.0 | `pydantic` is what pins `mcp` back, so bumping `fastapi` alone buys nothing. |
| **C. Bump both** | 0.141.1 | 2.13.5 | **1.30.0** | 1.6.0 | Newest SDK; matches `meeting-transcriber`'s verified set exactly. |

**Recommendation: A.** `mcp` 1.12.4 was checked for feature parity against
everything this plan needs and has all of it —
`FastMCP(streamable_http_path=…, transport_security=…, stateless_http=…,
json_response=…, instructions=…)` are all present in its `__init__`, and
`mcp.server.transport_security.TransportSecuritySettings` exists. Every
reproduction recorded in this document was run on **A** (`fastapi` 0.115.6 +
`pydantic` 2.10.4 + `mcp` 1.12.4 + `starlette` 0.41.3), so the
"verified" notes below describe the recommended configuration, not a different
one.

Option A keeps a security-audited image's dependency graph from moving for a
feature that is additive. Revisit if a concrete `mcp` ≥1.13 feature is needed;
at that point take **C** wholesale, since it is already running in production
in the sibling repo.

Whichever is chosen: **record the resolved versions in the PR description**,
and add a `pip check` (or equivalent) to `tests/run_tests.sh` so a future
resolver drift fails the suite instead of the tunnel.

---

## Decision gate 3 — does MCP belong in the hardened image at all?

The current image is `python:3.12-slim` + three packages, run `--read-only`,
`--cap-drop ALL`, `no-new-privileges`, non-root, 512 MB, 200 pids. Adding
thirteen transitive packages and a long-lived ASGI sub-application to it is a
real change in audit surface for a container whose job is to be the one thing
an autonomous agent can reach.

**Recommendation: make it optional, and off unless configured.** Two guards,
both cheap:

1. **Build-time.** Put `mcp` in its own `app/requirements-mcp.txt` and its own
   Dockerfile layer after `app/requirements.txt` (`Dockerfile:9-11`). Guard
   the import in `app/main.py`, as `meeting-transcriber`'s `app/main.py:713-720`
   does:

   ```python
   try:
       from . import mcp_endpoint as _mcp_endpoint
   except ImportError:
       _mcp_endpoint = None
       log.info("mcp not installed; /mcp endpoint disabled (REST API unaffected)")
   ```

   An image built without that layer still serves the REST API with `/mcp`
   simply absent. That also gives you a genuinely minimal variant to publish
   for anyone who wants today's audit surface.

2. **Run-time.** `ENABLE_MCP`, default **`false`**, in `Config`
   (`app/config.py:33`), read with the existing `_env_bool`. Mounting an MCP
   endpoint is a deliberate act, and a default-off flag means pulling the new
   image never silently widens what a running deployment exposes.

Report the state in `GET /v1/info` (`app/main.py:207`) — add `"mcp_enabled"`
so an operator can confirm what a deployment is actually serving without
probing.

---

## Implementation

Target file: **`app/mcp_endpoint.py`**, mirroring
`meeting-transcriber/app/mcp_endpoint.py` closely enough that a reader of one
recognises the other.

### Mounting, and the three gotchas that come with it

All three reproduce on this repo's pins; scripts to re-run them are sketched
inline.

**Gotcha 1 — the mounted sub-app's lifespan never runs.** Starlette does not
forward ASGI lifespan events into a mounted sub-application, so
`mcp.streamable_http_app()`'s own lifespan — which starts the session manager
— never executes. The server starts fine and then fails on the first tool
call. **Verified:**

```
app.mount("/mcp", mcp.streamable_http_app())
POST /mcp/ …  ->  RuntimeError: Task group is not initialized. Make sure to use run().
```

**Also verified: `stateless_http=True` does not exempt you.** It fails
identically. The session manager must be entered from *this app's* lifespan:

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    async with _mcp_session_scope():   # no-op when mcp is absent/disabled
        yield
```

**This repo has no lifespan to put it in.** `app/main.py:413` uses the
deprecated `@app.on_event("startup")` for the stale-temp-file sweep. Part of
this work is converting that to an `asynccontextmanager` lifespan passed to
`FastAPI(...)` at `app/main.py:58`, moving `VAULT.sweep_temp_files()` into it.
That conversion is worth doing on its own merits (`on_event` is deprecated in
current FastAPI) and should be its own commit so it can be reviewed
independently of the MCP work.

Note also that `mcp.session_manager` cannot be touched until
`streamable_http_app()` has been called once — it is built lazily — so the
mount must happen at import time, before the lifespan runs.

**Gotcha 2 — `streamable_http_path` must be `"/"`.** `streamable_http_app()`
registers its own route at that setting, which defaults to `/mcp`; mounting
that sub-app at `/mcp` with the default unchanged puts the real endpoint at
`/mcp/mcp`. Construct `FastMCP(..., streamable_http_path="/")`.

**Gotcha 3 — a bare `/mcp` 307s, and the connector refuses to follow it.**
`app.mount("/mcp", sub)` is a Starlette `Mount`, which compiles to
`"/mcp" + "/{path:path}"` and can never match the bare prefix; `redirect_slashes`
turns the resulting 404 into a 307 to `/mcp/`. claude.ai's backend probes the
configured URL **with the static auth header already attached** and will not
follow a redirect while doing so — sound behaviour, and it surfaces as
`static_headers_probe_redirect`. **Verified on these pins:**

```
POST /mcp   -> 307  location: http://testserver/mcp/
```

The fix, also **verified working on these pins** — an exact-path route
registered *before* the mount, rewriting `scope["path"]`:

```python
class _ExactPathForward:
    def __init__(self, app): self._app = app
    async def __call__(self, scope, receive, send):
        scope = dict(scope)
        scope["path"] = "/"
        await self._app(scope, receive, send)

app.add_route("/mcp", _ExactPathForward(sub_app), methods=["GET", "POST", "DELETE"])
app.mount("/mcp", sub_app)
```

With that in place both `POST /mcp` and `POST /mcp/` return `200` with a real
`initialize` result. It must be a callable **object**, not a plain function:
Starlette's `Route` detects `inspect.isfunction`/`ismethod` endpoints and wraps
them with `request_response()`, which expects `func(request) -> Response` and
would reject a raw ASGI callable.

**Gotcha 4 — the SDK's DNS-rebinding guard 421s real deployments.**
`TransportSecuritySettings(enable_dns_rebinding_protection=True)` is the SDK
default and only allows `Host: localhost` variants, so every request through
the tunnel hostname is rejected before reaching a tool. Add
`MCP_PUBLIC_HOSTNAME` to `Config`; when set, allow-list it and keep the guard
on; when unset, disable the guard outright **and log a warning once**, rather
than 421ing silently. `/mcp` is gated by the token check either way.

### Transport mode: stateless and JSON, not SSE

Recommendation: `FastMCP(..., stateless_http=True, json_response=True)`.

Rationale specific to this deployment: the container runs with `--memory 512m`
and `--pids-limit 200` behind a Cloudflare tunnel. Stateful mode holds a
session object per connector and an open SSE stream; Cloudflare will cut long
streams at its own timeout regardless, and idle sessions are unbounded state
in a container that is deliberately small. File operations here are
milliseconds — there is nothing to stream. Stateless request/response is the
honest shape.

**Verified:** `stateless_http=True` works for `initialize` and `tools/call`
over plain POSTs with no session id. **Unverified:** that the Cowork /
claude.ai connector is happy with a stateless server. It should be — the spec
supports it — but confirm during step 0 and record the answer here.

### Auth: reuse `_check_token`, via middleware

The mounted sub-app is a raw Starlette app, so FastAPI's `Depends` machinery
(`ReadAuth`/`WriteAuth`, `app/main.py:162-163`) does not apply to it. Gate the
whole surface with a `BaseHTTPMiddleware` wrapping the sub-app, exactly as
`meeting-transcriber`'s `_RequireAuth` does:

```python
class _RequireAuth(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        try:
            scope = _check_token(request.headers.get("authorization"))
        except HTTPException as exc:
            return JSONResponse(exc.detail, status_code=exc.status_code,
                                headers=exc.headers or {})
        request.state.token_scope = scope
        return await call_next(request)
```

This also means an unauthenticated request never reaches a tool, which is what
you want: no tool body should ever be the first thing to check a credential.

**Call `_rate_limit()` here too** (`app/main.py:116`). It is a module-global
deque shared with the REST path, and if the middleware skips it the MCP
surface is unlimited while the documented limit says 600/min. One MCP session
issues several JSON-RPC calls (`initialize`, `tools/list`, then calls), so
watch that the shared budget is not surprising in practice; if it is, that is
an argument for a separate counter, not for skipping the limit.

### Token scope inside a tool

`API_TOKEN_READONLY` must keep meaning something on this surface. A tool body
can reach the original request: **verified** that
`ctx.request_context.request` is the Starlette `Request`, carrying the
connector's headers, and that `request.state` set in the middleware survives
to it. So write tools take a `Context` and refuse early:

```python
@mcp.tool()
def write_file(path: str, content: str, ctx: Context, ...) -> dict:
    _require_write(ctx)   # raises with the same "This token is read-only" text
```

Mirror `require_write`'s message (`app/main.py:156-158`) verbatim so an
operator grepping logs sees one string, not two.

### The security regression this introduces, and how to not ship it

**This is the part of the plan most specific to this repo, and the easiest to
miss.**

`_path_error_handler` (`app/main.py:170-178`) is what makes every rejected
path visible: `log.warning("denied %s %s -> %s (%s)", ...)`, the thing
`README.md` promises shows up in `docker logs` and `tests/run_tests.sh:83`
counts.

**Verified: it will not fire for MCP calls.** FastMCP catches exceptions
raised inside a tool and converts them into a JSON-RPC result with
`isError: true` and HTTP **200**. The exception never propagates to the host
app's exception middleware:

```
POST /mcp/ tools/call  ->  200 {"result":{"content":[{"type":"text",
                                "text":"Error executing tool boom: traversal"}],
                                "isError":true}}
host exception handler ran: []
```

So a connector probing `../../secrets.env` would be rejected by `Vault` — the
containment itself is unaffected — but the attempt would be **invisible in the
logs**, silently weakening the observability half of the security model.

**Requirement:** the tool layer catches `PathError` itself and logs the same
`denied` line before translating it into a tool error. Put it in one shared
helper that every tool routes through, so the logging cannot be forgotten
per-tool:

```python
def _guard(op: str, fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except PathError as exc:
        log.warning("denied MCP %s -> %s (%s)", op, exc.code, exc.message)
        raise ValueError(f"{exc.code}: {exc.message}") from exc
```

Two related notes:

- The host's `_security_headers` middleware (`app/main.py:189`) **does** wrap
  mounted sub-apps, so `/mcp` responses get `nosniff`/`no-referrer`/`no-store`
  without further work. Worth asserting in a test rather than assuming.
- Error *messages* pass through to the caller verbatim (see the transcript
  above). That is the existing REST behaviour too and is fine — the messages
  are deliberately non-revealing — but re-read `safepath.py`'s message strings
  with "a model will read this out loud" in mind before shipping.

### Reuse, not a second implementation

`/mcp` is in-process. **Do not have it make HTTP calls to this app's own REST
API.** Tool bodies call `VAULT` directly (`app/main.py:42`) or the route
functions, which are plain functions under their decorators. Self-HTTP would
add a hop, a second token round-trip, and a second place for a path to be
parsed — the last of which is disqualifying on its own in this repo.

**Every path argument goes through `VAULT.components()` (`app/safepath.py:210`)
and nothing else.** No `os.path.join`, no f-string, no `Path(root) / arg`, at
any point, in any tool. This is the single invariant the whole security model
rests on, and a new front end is exactly where it gets broken by accident.

### Self-documenting the connector

`FastMCP(instructions=...)` is surfaced in the `initialize` result —
**verified**. `AGENT.md` is already a well-written brief for exactly this
audience, and its "This folder is a git repository" section is *load-bearing*:
`autocommit/` commits and pushes whatever appears, so an agent writing through
`/mcp` is writing to a public git history within `INTERVAL` seconds.

Put a condensed version of that warning in `instructions=` **and** in the
`write_file` / `delete_file` tool descriptions. A tool description is read at
the moment of use; `instructions` may not be.

`AGENT.md` is not currently copied into the image (`Dockerfile:28` copies only
`app/`), so the text has to live as a module constant in `app/mcp_endpoint.py`.
That is a duplication — record it in `AGENT.md` with a pointer, and keep
`AGENT.md` as the REST-fallback brief it already is.

---

## Tool surface

Nine tools, one per REST capability plus an info tool. Names are chosen to read
well in a tool list, not to mirror HTTP verbs.

| Tool | Maps to | Notes |
|---|---|---|
| `get_folder_info` | `GET /v1/info` (`app/main.py:207`) | Limits, denied names, whether delete is allowed, the token's scope (`rw`/`ro`), and the MCP-specific inline budgets. **Call this first** — say so in the description. |
| `list_files(path="", recursive=False, limit=1000)` | `GET /v1/list` (`app/main.py:220`) | Same `truncated` semantics. Keep `readable` in the output — it is how the model learns that symlinks exist and are refused. |
| `stat_file(path)` | `GET /v1/stat` (`app/main.py:237`) | |
| `read_file(path, encoding="utf-8", max_chars=None)` | `GET /v1/content` (`app/main.py:253`) | Budget + preview + explicit truncation note naming the REST URL. Binary refused unless `encoding="base64"`. See "The two hard limitations". |
| `write_file(path, content, encoding="utf-8", parents=True, overwrite=True)` | `PUT`/`POST /v1/content` (`app/main.py:328`, `:351`) | One tool, not two: `overwrite=False` is the `POST` semantics (409 if it exists). `parents` defaults to **`True`** here, unlike REST — the `409 no_parent` dance exists to protect a script from a typo, and a model that has just been told the path is not helped by it. Write-scope only. |
| `delete_file(path, recursive=False)` | `DELETE /v1/content` (`app/main.py:374`) | Honours `ALLOW_DELETE`. Description must state that deletion does not remove the file from git history. Write-scope only. |
| `make_directory(path, parents=True)` | `POST /v1/mkdir` (`app/main.py:391`) | Already idempotent; say so (`created: false`, not an error). Write-scope only. |
| `move_file(src, dst, overwrite=False, parents=False)` | `POST /v1/move` (`app/main.py:405`) | Write-scope only. |
| `find_files(pattern, path="", limit=200)` | *new* | `fnmatch` over `VAULT.walk()` (`app/safepath.py:364`). Not a REST endpoint. Optional, but it is the difference between a connector and a remote control — the same role `list_incoming_files` plays in `meeting-transcriber`. Add only if `list_files(recursive=True)` proves clumsy in real use. |

Deliberately **not** included, with reasons:

- **`append_file` / `edit_file`.** Tempting, because a model writing a long
  document is bounded by output tokens per call, and chunked appends are the
  obvious workaround. But atomic replace is a documented guarantee of this
  service (`README.md`: "a reader never sees a half-written file"), implemented
  as write-temp-then-`rename` (`app/safepath.py:410`), and there is no
  append that preserves it. Breaking that invariant for MCP ergonomics is a
  real trade and belongs in its own proposal, not smuggled in here. See
  open question 3.
- **Anything that reads outside the folder.** Obviously. Stated so that the
  absence is visibly deliberate.

---

## Tests

`tests/run_tests.sh` builds the image and runs `tests/escape_suite.py` against
two container configurations (`isolated` and `app-only`). **That structure is
the reason this repo can claim what it claims, and the MCP surface must be
inside it, not beside it.**

1. **`tests/mcp_escape_suite.py` — the non-negotiable one.** Every hostile path
   in `escapes()` (`tests/escape_suite.py:283`) driven through `/mcp`
   `tools/call` instead of the REST request line, with the same pass criterion:
   the canary stayed out of the response **and** the file outside the folder is
   byte-identical. The attack surface differs in a way that matters — paths
   arrive in a JSON body, so URL normalisation and double-encoding are not
   in play, but JSON string escapes are: `\u0000`, `\ud800` lone surrogates,
   overlong sequences, and `..` in every position must all be tried.
2. **Run it in both container modes.** Add it to `run_mode()`
   (`tests/run_tests.sh:67`) so `app-only` — the deliberately weakened layout
   with the whole repo mounted — proves the MCP surface holds on `Vault` alone.
   An MCP suite that only runs in `isolated` proves almost nothing.
3. **Denials are logged.** Assert the `denied MCP …` warning appears in
   `docker logs` after a rejected tool call. This is the test that catches the
   regression described above; write it first and watch it fail against a naive
   implementation.
4. **Read-only token.** Every write tool refused, with the same message text
   as `require_write` produces.
5. **Unauthenticated `tools/call`** gets 401 without reaching a tool; a correct
   token gets through. Both branches, per `meeting-transcriber`'s "test 10".
6. **Bare `/mcp` does not redirect.** `POST /mcp` returns 200, not 307 — the
   `static_headers_probe_redirect` regression test. Confirm it fails with the
   `_ExactPathForward` route removed.
7. **Budgets.** A file over `MCP_INLINE_LIMIT_CHARS` returns
   `truncated: true` with a byte count and the REST pointer; a binary file is
   refused with an explanatory message; `encoding="base64"` over its cap is
   refused.
8. **Import guard.** With `mcp` absent, the app still boots and serves REST
   with `/mcp` absent. With `ENABLE_MCP=false`, likewise. `meeting-transcriber`'s
   "test 9" notes that this is fiddlier than it looks — a subprocess hides the
   branch from coverage, and an in-process `sys.meta_path` blocker is
   insufficient on its own because the stale package attribute satisfies the
   import.
9. **Security headers** present on an MCP response (`nosniff`, `no-referrer`,
   `no-store`).
10. **`pip check`** after install, so a resolver drift that re-breaks the
    `starlette` bound fails the build rather than the tunnel.

Follow the existing style: a standalone script with a `Results` accumulator and
a non-zero exit, like `tests/escape_suite.py`. This repo has no pytest
convention and does not need one.

---

## Docs to update in the same PR

- **`README.md`** — an "MCP connector" section: what it is, both hard
  limitations stated plainly, the Cloudflare Access bypass, `ENABLE_MCP`, and
  the step-by-step for adding it in Cowork. The security model section needs a
  sentence on the MCP surface being the same `Vault` behind the same token.
- **`AGENT.md`** — a note at the top: if you have the connector, use the tools;
  this document is for the REST fallback and for large or binary files.
- **`.env.example`** — `ENABLE_MCP`, `MCP_PUBLIC_HOSTNAME`,
  `MCP_INLINE_LIMIT_CHARS`, `MCP_BASE64_LIMIT_BYTES`.
- **`unraid/claude-files.xml`** — the same variables as template fields.
- **`docker-compose.yml`** — the new env vars, commented and defaulted off.
- **This file** — replace "proposed" with what was actually built, record the
  step-0 findings (header names the dialog offers, whether stateless works),
  and note anything that turned out differently. That is the convention
  `meeting-transcriber`'s `CLAUDE.md` describes and it is what makes these
  documents worth writing.

---

## Open questions

1. **Which header does the connector dialog actually accept?** Decision gate 1.
   Answer empirically before writing code; the recommendation (accept both
   `Authorization: Bearer` and `X-API-Key`) is cheap insurance either way.
2. **Which dependency resolution — A, B or C?** Recommendation is **A**, hold
   every current pin and let `mcp` backtrack to 1.12.4, which was checked to
   have every feature this plan uses. Revisit only for a concrete newer-SDK
   need.
3. **Does the no-append rule survive contact with real use?** If writing long
   documents through the connector turns out to be genuinely painful, the
   honest fix is a `PATCH`-style endpoint designed with the atomicity guarantee
   intact (read-modify-write into the same temp-then-`rename` path, with an
   mtime or hash precondition to detect a concurrent write), exposed on both
   REST and MCP — not an append bolted onto the MCP layer. Own proposal, own
   file in `docs/`.
4. **Resources as well as tools?** MCP resources would let a file be *attached*
   rather than read into a tool result, which is a better fit for how a model
   wants to consume a document. Connector support for resources is uneven
   across surfaces, so: tools first, resources as a follow-on once the tool
   surface is proven.
5. **Should `find_files` ship in v1?** Only if `list_files(recursive=True)`
   proves clumsy in real use. Adding it later is easy; removing a tool from a
   published connector is not.
