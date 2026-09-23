# Feature request: an MCP connector surface for claude-files

**Status: implemented**, per this document as written -- `app/mcp_endpoint.py`
mounts `/mcp`, `app/main.py` is widened for `X-API-Key` and converted to a
proper `lifespan`, `requirements-mcp.txt` + the Dockerfile's `WITH_MCP` build
arg carry decision gate 2/3, and `tests/mcp_escape_suite.py` drives the same
containment suite as `tests/escape_suite.py` through `tools/call`. All three
decision gates below were built exactly as resolved (`X-API-Key`, Option A's
pinned `mcp==1.12.4` installed in one `pip` invocation, `ENABLE_MCP` defaulting
true alongside the `WITH_MCP` build arg).

**What was NOT re-verified while implementing this**, and is worth flagging
for whoever deploys it first: every "Verified" note this document inherited
from `meeting-transcriber`'s own build (the four mounting gotchas, the
`stateless_http=True` transport, `FastMCP(instructions=...)` surfacing) was
reproduced there against the real SDK, not re-reproduced here -- this
implementation trusts that prior verification rather than repeating it,
since the same `mcp==1.12.4` pin and the same mounting pattern are in play.
What genuinely has **not** been exercised against a live container or a real
claude.ai connector dialog in *this* repo is: `docker build`/`tests/run_tests.sh`
itself (no Docker daemon was available in the environment this was built in),
and the open questions below, which were already unresolved before this pass
and remain so. Run `tests/run_tests.sh` before treating this as
production-verified, and record real connector-setup findings in the section
below the first time it's added as a live connector.

The three gates are resolved as follows; each section below records the
reasoning:

| Gate | Resolution |
|---|---|
| **1. Auth header** | **`X-API-Key`.** Confirmed working in practice — it is what the `meeting-transcriber` connector runs on today. Widen `_check_token()` to accept it. |
| **2. Dependencies** | **Option A — hold every current pin** and let the resolver backtrack `mcp` to 1.12.4. No existing pin moves. |
| **3. Image surface** | Optional at build time (`WITH_MCP`) + `ENABLE_MCP` env var, **defaulting to `true`**. |

One thing changed while writing this up and is worth reading before the
Dockerfile is touched: **Option A only survives if `requirements.txt` and
`requirements-mcp.txt` are installed in a single `pip` invocation.** The
sibling repo's separate-layer pattern reproduces the exact `starlette`
conflict Option A exists to avoid. Measured both ways under decision gate 2.

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

  **Correction (`docs/plan-mcp-large-writes.md`): this 1 MiB figure was wrong,**
  and wrong for a specific reason worth naming so it isn't repeated. It was
  picked as a *context-cost* budget — how much base64 is tolerable to spend
  on one read — without asking the separate question of what a caller can
  actually *emit* in a single `write_file` call. Those are different
  ceilings. The real single-shot write ceiling, measured against a live
  incident, was roughly **5 KB of decoded bytes** — about 200× smaller — and
  it varies by model and surface, so it can't be hardcoded as a fixed
  constant at all. The shipped default is now 8 KiB, treated as advisory
  rather than authoritative, with `begin_write`/`write_chunk`/`commit_write`
  as the path for anything larger and `write_file`/`read_file` carrying
  `expect_bytes`/`expect_sha256`/`sha256` so a truncated call is caught
  instead of silently written short. See that document for the full
  incident and root-cause writeup.
- `write_file` takes `encoding: "utf-8" | "base64"` under the same two caps.

---

## Decision gate 1 — auth header: **resolved, `X-API-Key`**

**Resolved: replicate `meeting-transcriber`'s arrangement exactly.** That
connector runs on a static `x-api-key` header today and it works, so there is
nothing left to discover here and no reason to invent a second scheme. The
open question was only ever whether `Authorization` would *also* be accepted
as a custom header name; it does not need answering, because the header that
is known to work is available.

`_check_token()` (`app/main.py:127`) accepts exactly one credential shape:

```python
if not authorization or not authorization.startswith("Bearer "):
```

**The change:** widen it to also read the token from `X-API-Key`, mirroring
`meeting-transcriber`'s `app/auth.py`. Shape it so the REST path is untouched
and the precedence is explicit:

```python
def _check_token(authorization: str | None, api_key: str | None = None) -> str:
    """Return "rw" or "ro". Accepts `Authorization: Bearer <token>` (the
    documented REST credential) or `X-API-Key: <token>` (what the Claude
    custom-connector dialog sends -- it offers a static header with
    `x-api-key` as a preset name, and that is the arrangement the sibling
    meeting-transcriber connector has been running on).
    """
    presented = None
    if authorization and authorization.startswith("Bearer "):
        presented = authorization[7:].strip()
    elif api_key:
        presented = api_key.strip()
    if presented is None:
        raise HTTPException(401, detail={"code": "unauthorized",
                                         "message": "Missing bearer token"},
                            headers={"WWW-Authenticate": "Bearer"})
    ...
```

Both `require_read` and `require_write` (`app/main.py:147`, `:152`) grow a
second `Annotated[str | None, Header()]` parameter for it, and the middleware
in "Auth: reuse `_check_token`" below reads both headers off the raw request.

Keep **`Authorization: Bearer` as the documented primary** — every `curl` in
`README.md`, the `AGENT.md` brief, and any existing consumer stay correct
unchanged. `X-API-Key` is the connector's door, documented as such.

Two things to get right while touching this code:

- **Use `hmac.compare_digest` on the new path too.** The existing comparisons
  (`app/main.py:136`, `:138`) are constant-time and the new branch must land
  in the same comparison, not a shortcut `==` beside it.
- **Do not accept both headers in one request with different values.** If
  `Authorization` is present it wins and `X-API-Key` is ignored, per the
  `elif` above — no "try each until one matches", which would turn one request
  into two guesses against the rate limiter.

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

**Still worth recording during the first real connector setup:** whether the
dialog surfaces `x-api-key` as a preset or needs it typed as a custom header
name, and the exact URL that was entered. Append it to this section so the
next person setting up a second deployment does not re-derive it.

---

## Decision gate 2 — dependencies: **resolved, Option A (backtrack)**

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

Three resolutions were measured. All three are clean installs; **A is the one
to build.**

| | `fastapi` | `pydantic` | `mcp` | `starlette` | Verdict |
|---|---|---|---|---|---|
| **A. Hold every current pin** | 0.115.6 *(unchanged)* | 2.10.4 *(unchanged)* | **1.12.4** | 0.41.3 | Resolver backtracks `mcp` to fit. No existing pin moves. |
| **B. Hold `pydantic`, bump `fastapi`** | 0.141.1 | 2.10.4 *(unchanged)* | **1.12.4** | 1.6.0 | `pydantic` is what pins `mcp` back, so bumping `fastapi` alone buys nothing. |
| **C. Bump both** | 0.141.1 | 2.13.5 | **1.30.0** | 1.6.0 | Newest SDK; matches `meeting-transcriber`'s verified set exactly. |

**Resolved: A.** Keeping a security-audited image's dependency graph still for
a feature that is purely additive is worth more than the newest SDK. `mcp`
1.12.4 was checked for feature parity against everything this plan needs and
has all of it — `FastMCP(streamable_http_path=…, transport_security=…,
stateless_http=…, json_response=…, instructions=…)` are all present in its
`__init__`, and `mcp.server.transport_security.TransportSecuritySettings`
exists. Every reproduction recorded in this document was run on **A**
(`fastapi` 0.115.6 + `pydantic` 2.10.4 + `mcp` 1.12.4 + `starlette` 0.41.3),
so the "verified" notes below describe the configuration that will actually be
built, not a different one.

Revisit only for a concrete `mcp` ≥1.13 feature. At that point take **C**
wholesale rather than assembling a fourth combination — C is already running
in production in the sibling repo, which is the only reason to prefer it.

### What this means for the requirements files

**Pin `mcp` explicitly rather than relying on the backtrack.** `mcp>=1.0,<2.0`
happens to resolve to 1.12.4 *today* because `pydantic==2.10.4` and
`starlette<0.42.0` squeeze it there, but that is the resolver reaching a
conclusion, not a decision recorded in the repo. A future `mcp` 1.12.x patch,
or a `pydantic` bump made for an unrelated reason, would silently move it.
This repo tight-pins everything else for exactly this reason:

```
# app/requirements-mcp.txt
# Optional: only needed for the /mcp connector endpoint (ENABLE_MCP).
# Pinned, not ranged: 1.12.4 is the newest mcp that resolves against the
# fastapi/pydantic pins in requirements.txt without moving them -- see
# docs/plan-mcp-connector.md, "Decision gate 2". Bumping this may drag
# starlette past fastapi's declared bound; check with `pip check`.
mcp==1.12.4
```

### Option A only holds if it resolves in **one** `pip install`

**Verified, and it dictates the Dockerfile.** The sibling repo installs its
`requirements-mcp.txt` as a *separate* layer, specifically to keep the
resolver away from its ML pins. Copying that pattern here **reproduces the
exact break Option A was chosen to avoid**, because `mcp` and `fastapi` share
`starlette` and pip resolves each invocation independently — it does not treat
already-installed packages as constraints:

```
# two invocations -- the sibling repo's pattern
$ pip install fastapi==0.115.6 "uvicorn[standard]==0.34.0" pydantic==2.10.4
$ pip install mcp==1.12.4
$ pip check
fastapi 0.115.6 has requirement starlette<0.42.0,>=0.40.0, but you have starlette 1.6.0.
```

`sse-starlette` floats to 3.4.11, which drags `starlette` to 1.6.0, straight
back through `fastapi`'s bound — **even with `mcp` itself pinned to 1.12.4.**

One invocation, same pins, is clean:

```
# one invocation
$ pip install fastapi==0.115.6 "uvicorn[standard]==0.34.0" pydantic==2.10.4 mcp==1.12.4
$ pip check
No broken requirements found.
```

So the build stage installs both files together, and the optional-ness is a
build arg rather than a separate layer (`Dockerfile:9-11`):

```dockerfile
ARG WITH_MCP=1
COPY app/requirements.txt app/requirements-mcp.txt ./
RUN python -m venv /venv \
 && if [ "$WITH_MCP" = "1" ]; then \
      /venv/bin/pip install --no-cache-dir -r requirements.txt -r requirements-mcp.txt; \
    else \
      /venv/bin/pip install --no-cache-dir -r requirements.txt; \
    fi \
 && /venv/bin/pip check
```

The `pip check` in the build is deliberate: it makes a bad resolve fail the
image build rather than surface as an import error in production. Losing the
separate cache layer is a real but small cost — this is a three-package base,
not an ML stack.

**Record the full resolved set in the PR description**, and add a `pip check`
to `tests/run_tests.sh` too, so drift fails the suite and not just the build.
The expected set on A:

| Package | Version | Source |
|---|---|---|
| `fastapi` | 0.115.6 | `requirements.txt` (unchanged) |
| `uvicorn[standard]` | 0.34.0 | `requirements.txt` (unchanged) |
| `pydantic` | 2.10.4 | `requirements.txt` (unchanged) |
| `mcp` | 1.12.4 | `requirements-mcp.txt` (new) |
| `starlette` | 0.41.3 | pulled in by `fastapi`/`mcp` — **the one to watch** |
| `sse-starlette` | 3.0.3 | pulled in by `mcp` — what drags `starlette` if it floats |
| `httpx` / `httpx-sse` | 0.28.1 / 0.4.3 | pulled in by `mcp` |
| `anyio` | 4.15.1 | pulled in by `mcp`/`starlette` |
| `pydantic-settings` | 2.15.0 | pulled in by `mcp` |
| `jsonschema` | 4.26.0 | pulled in by `mcp` |
| `python-multipart` | 0.0.32 | pulled in by `mcp` |

---

## Decision gate 3 — image surface: **resolved, optional build + `ENABLE_MCP=true`**

The current image is `python:3.12-slim` + three packages, run `--read-only`,
`--cap-drop ALL`, `no-new-privileges`, non-root, 512 MB, 200 pids. Adding an
ASGI sub-application and **14 new packages** — measured, not estimated:
`mcp`, `httpx`, `httpcore`, `httpx-sse`, `sse-starlette`, `jsonschema`,
`jsonschema-specifications`, `referencing`, `rpds-py`, `attrs`, `certifi`,
`pydantic-settings`, `python-multipart`, `typing-inspection` — is a real
change in audit surface for a container whose job is to be the one thing an
autonomous agent can reach.

Worth noting what is in that list: `httpx`/`httpcore`/`certifi` are an **HTTP
client** stack, in an image that previously had no way to make outbound
requests at all. `mcp` pulls them for its client half, which this server never
uses. Nothing here *calls* them, and the container has no business reaching
out, so this is worth a line in the README's security model rather than a
veto — but an egress-restricted deployment is now a meaningfully different
proposition than it was, and that should be said out loud rather than
discovered.

**Resolved: keep both guards, but ship it on.** Two mechanisms:

1. **Build-time.** `app/requirements-mcp.txt`, installed in the same `pip`
   invocation as `requirements.txt` under `ARG WITH_MCP` (see "Option A only
   holds if it resolves in one `pip install`" above — *not* a separate layer).
   Guard the import in `app/main.py`, as `meeting-transcriber`'s
   `app/main.py:713-720` does:

   ```python
   try:
       from . import mcp_endpoint as _mcp_endpoint
   except ImportError:
       _mcp_endpoint = None
       log.info("mcp not installed; /mcp endpoint disabled (REST API unaffected)")
   ```

   An image built with `--build-arg WITH_MCP=0` still serves the REST API with
   `/mcp` simply absent. That gives you a genuinely minimal variant to publish
   for anyone who wants today's audit surface, and it is what the import guard
   is for.

2. **Run-time.** `ENABLE_MCP`, default **`true`**, in `Config`
   (`app/config.py:33`), read with the existing `_env_bool`. The connector is
   the point of the feature; making every deployment set a flag to get the
   thing it just pulled the image for is friction without a matching benefit,
   given that `/mcp` is gated by the same token as every other route and by
   the same `Vault`.

   Two consequences to be deliberate about, since default-on is the less
   conservative choice:

   - **Pulling a new `:latest` starts serving `/mcp` on an existing
     deployment.** The unRAID templates track `:latest` (`README.md`,
     "Publishing a new version"), so this will happen without anyone asking
     for it. It is defensible — the endpoint is unreachable without the
     token — but it belongs in the release notes for that version, stated
     plainly, not discovered.
   - **`ENABLE_MCP=false` must be a real off switch**, not cosmetic: when
     false, `mount_mcp()` is never called and neither route is registered, so
     `/mcp` 404s exactly as it does on an image built without `mcp`. Test it
     (test 8 below) rather than assuming the flag is threaded all the way
     through.

   Deployments that want the old surface set `ENABLE_MCP=false`, or run the
   `WITH_MCP=0` image and get it at build time.

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

**Gotcha 5 -- found during this implementation, not in the original plan:
`mcp==1.12.4` cannot register a tool from a module with `from __future__
import annotations`.** Every other module in this repo uses it; `app/mcp_endpoint.py`
deliberately does not, and says why inline. `Tool.from_function()`
(`mcp/server/fastmcp/tools/base.py`) calls `inspect.signature(fn)` without
`eval_str=True`, then `issubclass(param.annotation, Context)` on whatever it
gets. With PEP 563 active, every parameter's annotation is a plain string
(`"str"`, `"int | None"`, ...) rather than a real type object, and
`issubclass("str", Context)` raises `TypeError: issubclass() arg 1 must be a
class` -- for *any* parameter, not just `Context`- or union-typed ones.
**Verified directly against the pinned SDK**: a single-parameter `def
plain(path: str)` tool fails to register with the future import present and
registers fine without it. This was not caught by this document's earlier
"verified" notes because none of them exercised actual tool *parameter*
registration (`@mcp.tool()` on a function with arguments) -- they were all
about mounting, routing, and auth. Since this module targets Python 3.12
(the Dockerfile's base image), dropping the future import costs nothing:
`X | None` and `list[...]` are native syntax there.

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
            scope = _check_token(
                request.headers.get("authorization"),
                request.headers.get("x-api-key"),   # what the connector sends
            )
        except HTTPException as exc:
            return JSONResponse(exc.detail, status_code=exc.status_code,
                                headers=exc.headers or {})
        request.state.token_scope = scope
        return await call_next(request)
```

This also means an unauthenticated request never reaches a tool, which is what
you want: no tool body should ever be the first thing to check a credential.

The `WWW-Authenticate: Bearer` header `_check_token` raises with
(`app/main.py:133`) is now slightly misleading on this surface, since the
connector authenticates with `X-API-Key`. Leave it — it is correct for the
REST path, harmless here, and the 401 body already carries a `code` and
`message` that say what happened.

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

Eight tools, one per REST capability plus an info tool -- the ninth, `find_files`,
was deferred; see open question 5. Names are chosen to read
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
8. **Both off switches, separately.** With `mcp` absent (a `WITH_MCP=0`
   image), the app boots and serves REST with `/mcp` absent. With `mcp`
   installed but `ENABLE_MCP=false`, `/mcp` **404s** — assert the status, not
   just that no tool ran, since the failure mode of a half-threaded flag is a
   mounted-but-unlisted endpoint. `meeting-transcriber`'s "test 9" notes the
   import-guard half is fiddlier than it looks: a subprocess hides the branch
   from coverage, and an in-process `sys.meta_path` blocker is insufficient on
   its own because the stale package attribute satisfies the import.
9. **`X-API-Key` on the REST path too.** Widening `_check_token` touches every
   authenticated route, so `tests/escape_suite.py`'s functional section
   (`tests/escape_suite.py:105`) should cover: `X-API-Key` accepted where
   `Authorization` is today, the read-only token still read-only through it,
   a wrong `X-API-Key` still 401, and `Authorization` winning when both are
   present with different values.
10. **Security headers** present on an MCP response (`nosniff`, `no-referrer`,
    `no-store`).
11. **`pip check`** in both the image build and the suite, so a resolver drift
    that re-breaks the `starlette` bound fails loudly. This is the regression
    test for the two-invocations trap in decision gate 2 — worth writing
    against a deliberately split install once, to see it catch the thing it
    exists for.

Follow the existing style: a standalone script with a `Results` accumulator and
a non-zero exit, like `tests/escape_suite.py`. This repo has no pytest
convention and does not need one.

---

## Docs to update in the same PR

- **`README.md`** — an "MCP connector" section: what it is, both hard
  limitations stated plainly, the Cloudflare Access bypass, `ENABLE_MCP`, and
  the step-by-step for adding it in Cowork (URL is `https://<host>/mcp`, auth
  is "No sign-in" plus a static `x-api-key` header holding `API_TOKEN`). The
  security model section needs a sentence on the MCP surface being the same
  `Vault` behind the same token. The `Configuration` table gains the four new
  variables; the API table gains `/mcp`.
- **The version's release notes** — that pulling this image starts serving
  `/mcp` by default, and that `ENABLE_MCP=false` turns it off. See decision
  gate 3.
- **`AGENT.md`** — a note at the top: if you have the connector, use the tools;
  this document is for the REST fallback and for large or binary files. Also
  mention `X-API-Key` as an accepted alternative to `Authorization: Bearer`.
- **`.env.example`** — `ENABLE_MCP` (with `true` shown as the default and a
  comment saying what turning it off does), `MCP_PUBLIC_HOSTNAME`,
  `MCP_INLINE_LIMIT_CHARS`, `MCP_BASE64_LIMIT_BYTES`.
- **`unraid/claude-files.xml`** — the same variables as template fields.
  `MCP_PUBLIC_HOSTNAME` in particular wants to be visible there, since the
  tunnel hostname is exactly the thing an unRAID user knows and the SDK's
  rebinding guard is otherwise a silent 421.
- **`docker-compose.yml`** — the new env vars, commented, at their defaults.
- **This file** — replace "proposed" with what was actually built, record the
  connector-setup findings, and note anything that turned out differently.
  That is the convention `meeting-transcriber`'s `CLAUDE.md` describes and it
  is what makes these documents worth writing.

---

## Open questions

Gates 1, 2 and 3 are settled — see the table at the top and their sections.
What remains open:

1. **Does the connector accept a stateless server?** The only "unverified"
   item left in the implementation. It should — the spec supports it, and the
   recommendation (`stateless_http=True`, `json_response=True`) suits a
   container this small behind a tunnel. If it does not, fall back to stateful
   and record here what the failure looked like; nothing else in the design
   depends on it.
2. **Does the shared rate-limit budget hold up?** `_rate_limit()` is one
   600/min deque across REST and MCP, and an MCP session spends several calls
   on handshake before doing any work. If a connector session starts 429ing in
   normal use, the fix is a separate counter for `/mcp`, not a higher shared
   limit — say so here when it happens.
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
5. **Should `find_files` ship in v1?** **Resolved for this pass: no.** It did
   not ship -- there is no real usage yet to judge `list_files(recursive=True)`
   clumsy by, and the whole point of leaving it out was that adding it later
   is cheap while removing a published tool is not. Revisit once the eight
   shipped tools have seen real use.
