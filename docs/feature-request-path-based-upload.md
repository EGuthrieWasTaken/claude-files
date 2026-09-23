# Feature request: path-based upload (a local bridge), so binary files never pass through a model's output

**Status: proposed.** Nothing here is built yet. This document is written to be
implemented without further design work — every file, signature, header, error
string, and test is named. Where a decision could reasonably go either way, the
decision is made here and the losing option is recorded at the end rather than
left open.

Follows [`docs/plan-mcp-connector.md`](plan-mcp-connector.md) (shipped) and
[`docs/plan-mcp-large-writes.md`](plan-mcp-large-writes.md) (shipped in commit
`009633b`, **but see "Precondition" below — it is not deployed**).

**Build in three parts, in this order:** Part 0 is a deploy, not a code change,
and it must happen first or the rest is untestable against reality. Part 1 is
the bridge itself and is the feature. Part 2 is the small server-side surface
that makes the bridge's integrity promise end-to-end and makes the bridge
discoverable from inside the connector.

---

## The report

From an agent that generated a PDF in its own workspace and tried to archive it
through the connector, quoted at length because the diagnosis is correct and the
reasoning is the thing worth preserving:

> `write_file` only accepts the file's bytes inline, as a `content` string I
> write out myself in the tool call. For a PDF, that means base64: today's brief
> needed 41,232 base64 characters to represent 30KB of bytes. Base64 is about
> the worst case for how I generate text — it's high-entropy, no repeated words
> or patterns, so it tokenizes at close to one token per character, versus
> roughly four characters per token for normal prose or code. Whatever my
> per-turn output ceiling actually is, base64 hits it almost immediately: every
> attempt (three separate ones, including a completely clean turn with nothing
> else in it) cut off at almost exactly the same ~8,700 characters. That's
> consistent with a fixed token budget on my side, not a fluke and not something
> the connector's stated limits (100,000 chars inline, 1MB base64) were ever
> gating — those never came close to mattering.
>
> So "update the connector's limits" doesn't touch this, because the limit
> that's actually binding isn't a connector setting — it's that the tool
> requires the payload to pass through my generation. The fix has to be a
> different upload primitive, not a bigger number somewhere.

It then asked for, in preference order, (1) a path-based upload — "a
`write_file` variant (or new tool) that takes a local file path in my cloud
workspace and reads/transmits those bytes directly, the same pattern already
used elsewhere in my toolset: the Artifact tool's `file_path`, `SendUserFile`'s
`files` list, and the device-bridge's `device_commit_files`" — or failing that,
(2) a chunked upload.

Two facts about that report set up everything below:

- **The generation ceiling is real and is the binding constraint.** ~8,700
  characters, hit three times at the same offset including on an otherwise-empty
  turn. Note this is a *different* number from the ~6,500 characters the
  previous incident measured (`docs/plan-mcp-large-writes.md`, "The incident").
  Two surfaces, two ceilings, neither one a constant the server could ever
  hardcode. That is the whole argument for never routing bytes through
  generation when there is any alternative.
- **The agent reported the connector's limits as "100,000 chars inline, 1MB
  base64".** Those are the *pre-`009633b`* numbers. The agent was not looking at
  a current server, which is Part 0.

---

## Part 0 — Precondition: the deployed server is `1.0.0`, predating the chunked-write work

This is first because it changes what "still isn't working properly" means, and
because shipping Part 1 onto a stale deployment reproduces the complaint a third
time.

**Evidence, collected live from the connector while writing this document:**

| Check | Live server | This repo at HEAD |
|---|---|---|
| `get_folder_info` → `mcp_base64_limit_bytes` | `1048576` (1 MiB) | `8192` |
| `get_folder_info` → `mcp_upload_chunk_advisory_bytes` | *absent* | present |
| Tools exposed | 8 | 13 |
| `begin_write` / `write_chunk` / `commit_write` / `abort_write` | absent | present |
| `copy_file` | absent | present |
| `get_folder_info` description | ends "...`read_file` and `write_file` operate under." | "...and the chunk size `begin_write`/`write_chunk` advise." |

The raw result from the live connector:

```json
{"max_upload_bytes":268435456,"max_list_entries":10000,
 "denied_names":[".git",".gitmodules",".ssh"],"allow_delete":true,
 "allow_hardlinked_files":false,"token_scope":"rw",
 "mcp_inline_limit_chars":100000,"mcp_base64_limit_bytes":1048576}
```

That is exactly the output `docs/plan-mcp-large-writes.md` quotes as the *bug*.
The integrity floor — the `expect_bytes`/`expect_sha256` verification that makes
a truncated write a clean refusal instead of a silently corrupt file — **is not
running in production.** Neither are the chunked tools the reporting agent
independently re-invented ("something like `upload_chunk` + `finalize_upload`"),
which is itself evidence it never saw them.

**Why:** `docker-compose.yml:3` pins `eguthriewastaken2/claude-files:1.0.0`, and
`009633b` did not bump `app/main.py:99` (`version="1.0.0"`) or publish a new
image. The unRAID templates track `:latest`, so whichever of those the live
deployment uses, nothing republished means nothing changed.

**Do this before anything else:**

1. Bump `app/main.py:99` to `version="1.1.0"` (this change ships under it too).
2. Build and push per README "Publishing a new version", both tags:
   ```bash
   docker buildx build --platform linux/amd64,linux/arm64 \
     --tag eguthriewastaken2/claude-files:1.1.0 \
     --tag eguthriewastaken2/claude-files:latest --push .
   ```
3. Bump the `docker-compose.yml` pins to `:1.1.0`.
4. Redeploy, then **verify from a client, not from the host**: `GET /health`
   reports `"version":"1.1.0"`, `get_folder_info` reports
   `mcp_base64_limit_bytes: 8192` and a `mcp_upload_chunk_advisory_bytes` key,
   and the connector lists `begin_write`. If a claude.ai connector still shows
   the old tool list after redeploy, its tool cache needs refreshing — remove
   and re-add the connector, and say so in the README.

Version bumping is not bookkeeping here. `/health` reporting a version is the
only way to answer "is the thing I'm debugging even the code I'm reading",
which cost this project one full incident cycle.

**Part 0 does not make Part 1 unnecessary.** With the chunked tools deployed,
that 30 KB PDF is `begin_write` + ~10 `write_chunk` calls + `commit_write` —
about a dozen round trips, every byte still generated as base64 by the model,
every one of them still subject to the ceiling that caused this. It works, but
it is the fallback, not the fix. The agent's first preference is the right one.

---

## Why "add a `local_path` argument to `write_file`" cannot work

Read this before touching `app/mcp_endpoint.py`, because the obvious
implementation of the request is both impossible and, if attempted, a
containment breach.

`/mcp` is a **Streamable HTTP MCP endpoint mounted on the same FastAPI app as
the REST API** (`app/mcp_endpoint.py:199` `mount_mcp`), running in a container on
the unRAID host whose only filesystem is the bind-mounted shared folder. The
agent runs somewhere else entirely — a cloud workspace, a laptop, a phone. The
two share nothing but HTTPS.

In the MCP tool-call path, **every argument is JSON the model writes**. There is
no side channel that carries bytes. So:

- `write_file(local_path="/home/user/brief.pdf")` sent to this server would ask
  the *server* to open `/home/user/brief.pdf` — a path on the unRAID container,
  outside the vault. That is an arbitrary-file-read primitive bolted onto a
  service whose entire premise is that `app/safepath.py` confines every path to
  the shared folder. **Never add a parameter that makes the server open a path
  it did not resolve through `Vault.components`.** If a future implementer reads
  only the feature title and nothing else, this is the sentence that matters.
- The tools the reporting agent cited as precedent — `Artifact`'s `file_path`,
  `SendUserFile`'s `files`, `device_commit_files` — are implemented by the
  *client runtime*, in the same process tree as the workspace filesystem. They
  are path-based because the code reading the path and the file being read are
  on the same machine. claude-files is the odd one out not because it made a
  worse API choice, but because it is the only one of those that is a remote
  server.

**Therefore: path-based upload requires code running in the agent's own
environment.** That code already has a byte-transfer channel to use —
`PUT /v1/content/{path}` streams an arbitrarily large raw body, no
output-token ceiling anywhere in it (`app/main.py:354` `_write_body`). The
missing piece was never a server capability. It is a client the agent can
invoke in one tool call.

---

# Part 1 — `bridge/claude_files_bridge.py`

One file. Stdlib-only for everything except the optional stdio-MCP mode. Runs in
the agent's environment, reads the local file, streams it to the REST API, and
verifies what landed.

Three ways to run it, one implementation underneath:

| Mode | For | Invocation |
|---|---|---|
| **stdio MCP server** | Claude Code (desktop, cloud sessions, IDE), anything that can launch a local MCP server | `python3 claude_files_bridge.py serve` |
| **CLI** | any surface with a shell but no local-MCP support | `python3 claude_files_bridge.py upload ./brief.pdf briefs/2026/brief.pdf` |
| **import** | tests, and anyone scripting it | `from claude_files_bridge import upload` |

The stdio MCP mode is the deliverable the report asked for: it puts
`upload_file(local_path=..., remote_path=...)` in the agent's tool list, with the
bytes moving out of band. The CLI mode is the same code reachable from a
`Bash`-style tool, which is what a surface without local-MCP support has.

## Layout and dependencies

```
bridge/
  claude_files_bridge.py    # the whole thing, stdlib-only at import time
  README.md                 # 30 lines: install, configure, the three modes
  requirements.txt          # mcp==1.12.4   (ONLY needed for `serve`)
```

- **Stdlib-only at import time.** `import mcp` happens *inside* the `serve`
  subcommand, never at module top level. `upload`, `download`, and `check` must
  work on a bare `python3` with nothing installed — that is what makes the CLI
  mode a one-curl fix on an unknown surface.
- `mcp==1.12.4` in `bridge/requirements.txt`, the same pin as
  `app/requirements-mcp.txt`, for the same reason given there. The bridge is a
  *client-side* MCP server, so it shares no dependency graph with the app, but
  pinning the same version keeps one SDK version in the project's head.
- Python 3.9+ (`from __future__ import annotations`, no match statements, no
  `X | Y` at runtime in signatures evaluated eagerly). The app targets 3.12; the
  bridge lands on machines we do not control, so hold it lower deliberately and
  say so in a comment.

## HTTP, and the two things that will otherwise break in a cloud workspace

Use `urllib.request`, **not** `http.client`, and not `requests` (unavailable on a
bare interpreter). Two reasons, both load-bearing in the environment this is
meant for:

1. **Proxies.** Claude cloud sessions route outbound HTTPS through an agent
   proxy declared in `HTTPS_PROXY`. `urllib.request` honours `HTTPS_PROXY` /
   `HTTP_PROXY` / `NO_PROXY` automatically via `urllib.request.getproxies()`;
   `http.client` does not. A bridge that ignores the proxy fails to connect in
   exactly the environment this feature exists for.
2. **Custom CA bundles.** The same environments hand out a private CA. Build the
   SSL context as `ssl.create_default_context()`, then, if
   `SSL_CERT_FILE` or `REQUESTS_CA_BUNDLE` is set and names a readable file,
   `ctx.load_verify_locations(that_file)`. Honour them in that order, first one
   wins.

**There is no flag to disable TLS verification, deliberately.** Not
`--insecure`, not an env var. If verification fails the bridge says which CA
bundle it used and stops. Anything else turns a credential-bearing uploader into
a downgrade attack waiting for a bad network.

Streaming: open the file `"rb"` and pass the file object as `data=`, with an
explicit `Content-Length` header from `os.fstat(f.fileno()).st_size`. `urllib`
raises on an iterable body with no declared length, so the header is mandatory,
not optional. Never `f.read()` the whole file into memory — a 200 MB upload must
not need 200 MB of RAM.

## Configuration — environment only, never arguments

| Variable | Required | Meaning |
|---|---|---|
| `CLAUDE_FILES_URL` | yes | Base URL, e.g. `https://files.example.com`. Trailing slash tolerated and stripped. |
| `CLAUDE_FILES_TOKEN` | one of these two | The `API_TOKEN` (rw) or `API_TOKEN_READONLY` (ro — download only). |
| `CLAUDE_FILES_TOKEN_FILE` | one of these two | Path to a file whose *stripped* contents are the token. Wins if both are set. |
| `CLAUDE_FILES_TIMEOUT` | no | Per-request socket timeout in seconds, default `60`. |

**The token is never a tool argument and never a CLI argument.** Not
`--token`, not `upload_file(token=...)`. A tool argument is model-generated text
that lands in a transcript, a log, and a context window; the whole point of
`CLAUDE_FILES_TOKEN_FILE` is to keep the secret out of the MCP config JSON as
well, since an agent can read its own config. Sending it as a header is the only
place it appears.

Send it as `Authorization: Bearer <token>`. `app/main.py:175` `_check_token`
accepts `X-API-Key` too, but `Authorization` is the documented primary and wins
when both are present; the bridge is a first-class REST client, so it uses the
primary.

If the token is missing or `CLAUDE_FILES_URL` is unset, fail with a message
naming the variable and pointing at `bridge/README.md` — not a `KeyError`.

## Remote path handling

`remote_path` is relative to the shared folder root, always. Before building the
URL:

1. Split on `/`, drop empty segments (so `//a//b/` → `["a","b"]`).
2. Refuse locally, with a clear message, if any segment is `.` or `..`, or
   contains a NUL. The server refuses these too (`app/safepath.py`), and the
   server's refusal is the one that matters for security — this is purely so the
   agent gets a legible error without a round trip. **Do not** add any other
   client-side path policy: containment is the server's job and duplicating it
   in a client that anyone can edit invites the two copies to disagree.
3. `urllib.parse.quote(segment, safe="")` each segment and join with `/`. Quoting
   per-segment (rather than quoting the whole path) is what keeps a literal `#`,
   `?`, or space in a filename from becoming a URL structure character while
   leaving the separators intact.

Final URL: `{base}/v1/content/{quoted}` plus `?parents=true` when requested.

## The upload core

```python
def upload(local_path, remote_path, *, overwrite=True, parents=True):
    """Stream a local file into the shared folder. Returns a result dict."""
```

Sequence, in order:

1. `os.stat` the local path. Refuse a directory ("is a directory; upload one
   file, or tar it first"), refuse a missing file, refuse an unreadable one.
   Symlinks are followed — this is the agent's own filesystem, not a vault.
2. Read the file once in 1 MiB blocks to compute `sha256` and confirm the size.
3. Fetch `GET /v1/info` (read auth) once and compare against
   `max_upload_bytes`. Over it, refuse locally, naming both numbers, rather than
   streaming 300 MB to earn a `413`. Cache the result for the process lifetime;
   in `serve` mode, re-fetch if it is older than 5 minutes.
4. `PUT` (when `overwrite=True`) or `POST` (when `overwrite=False`) to
   `/v1/content/{path}`, streaming the file object, with headers:
   - `Authorization: Bearer …`
   - `Content-Type: application/octet-stream`
   - `Content-Length: <size>`
   - `X-Expect-Bytes: <size>`
   - `X-Expect-Sha256: <hex>`        ← verified server-side; see Part 2
   - `User-Agent: claude-files-bridge/1.1.0`
5. On `2xx`, parse the JSON body (`OkResponse`) and **compare
   `body["sha256"]` to the locally computed digest**. A mismatch — which Part 2
   makes nearly impossible — is reported as an integrity failure, not a success.
   If the server did not return a `sha256` (an older deployment, Part 0 not
   done), set `"verified": false` and include a note saying the server is
   pre-1.1.0 and the upload could not be confirmed end to end. Do not pretend it
   was verified.
6. Return:
   ```python
   {"ok": True, "remote_path": "briefs/2026/brief.pdf", "size": 30411,
    "sha256": "9f86d0…", "verified": True, "created": True,
    "url": "https://files.example.com/v1/content/briefs/2026/brief.pdf"}
   ```

The hash-then-upload order means a file rewritten between step 2 and step 4 is
caught by the server's `X-Expect-Sha256` check and refused — the race becomes a
clean refusal rather than a wrong file with a confident digest.

### Retries

Retry **only** transport failures — `URLError` wrapping a socket/DNS/TLS error,
or HTTP `502`/`503`/`504` — at most 3 retries, sleeping 2s, 4s, 8s. Re-`seek(0)`
the file handle before each attempt. **Never retry a 4xx**: a `403 readonly`, a
`409 exists`, or a `413 too_large` is an answer, not a hiccup.

`PUT` with `overwrite=True` is idempotent, so a retry after a mid-stream drop is
safe. With `overwrite=False` the request is a `POST`, and a retry that comes back
`409 exists` may mean the first attempt actually landed — report that case
explicitly ("the server says this path now exists; the first attempt may have
succeeded — `stat` it and compare the size before retrying") rather than as a
flat failure.

## The download core

Symmetric, and cheap once the upload path exists. It matters for the same reason
uploads do: pulling an existing PDF back out through `read_file(encoding=
"base64")` spends the whole tool result on bytes the model cannot read.

```python
def download(remote_path, local_path, *, overwrite=False):
```

`GET /v1/content/{path}`, streamed in 256 KiB blocks to `local_path + ".part"`,
hashed on the way through, then `os.replace()` into place — the same
write-temp-then-rename discipline the server uses, so a killed download never
leaves a half-file wearing the real name. Refuse to clobber an existing
`local_path` unless `overwrite=True`. Return the same shape as `upload`.

## `check`

```python
def check() -> dict
```

Diagnoses a broken setup in one call instead of four: reports the resolved base
URL, whether a token was found and **from which variable** (never its value),
`GET /health`'s status and version, `GET /v1/info`'s `max_upload_bytes` and
`mcp_enabled`, the proxy in use if any, the CA bundle in use if not the default,
and the token scope (inferred: a `403 readonly` from a harmless probe means
read-only — probe with `POST /v1/mkdir/.bridge-probe` only if explicitly asked,
otherwise just report what `/v1/info` gives). Every failure mode above gets a
one-line remedy in the output.

This exists because the last two iterations of this problem were both diagnosed
from the wrong side of the network.

## MCP tools (`serve`)

```python
mcp = FastMCP("claude-files-bridge")

@mcp.tool()
def upload_file(local_path: str, remote_path: str,
                overwrite: bool = True, parents: bool = True) -> dict: ...

@mcp.tool()
def download_file(remote_path: str, local_path: str,
                  overwrite: bool = False) -> dict: ...

@mcp.tool()
def bridge_status() -> dict: ...
```

Each is a thin wrapper that calls `upload` / `download` / `check` and lets the
exception text become the tool error. **Keep the core functions free of any
`mcp` import or `Context` argument** — that is what lets the test suite call them
directly without the SDK installed.

Three tools, not thirteen. The bridge does not mirror `list_files`, `stat_file`,
`read_file`, `delete_file`, or the chunked-write tools: the remote connector
already does all of those well, the model *wants* text results in its context for
those, and two implementations of the same surface drift. The bridge exists
solely for the case the connector structurally cannot serve — bulk bytes.

### Tool descriptions matter as much as the code

The previous incident's agent did everything the docstrings told it to. Write
these to be acted on:

- `upload_file`: "Upload a local file into the shared claude-files folder by
  path. **The bytes are read from disk and streamed directly — they never pass
  through your output, so there is no size ceiling from your side.** Use this for
  any binary file (PDF, image, archive) and for any text file over a few KB,
  instead of the connector's `write_file`, which requires you to type the content
  out. `remote_path` is relative to the shared folder root. Returns the size and
  a sha256 verified by the server. Remember: that folder is a git repository and
  writes are committed and pushed automatically — never upload credentials or
  secrets."
- `download_file`: "Fetch a file from the shared folder onto local disk by path,
  without its bytes passing through your context. Use this instead of
  `read_file(encoding='base64')` for anything binary or large."
- `bridge_status`: "Check that the bridge can reach the server: base URL, token
  presence and scope, server version, size limits. Call this first if an upload
  fails."

## stdout discipline

In `serve` mode **stdout is the MCP transport**. A stray `print()` corrupts the
protocol stream and the failure looks like the server hanging, not like a typo.
Every diagnostic in the module goes to `stderr` via `logging` configured with a
`StreamHandler(sys.stderr)`. The CLI's own human output goes to stdout only in
CLI mode, and `--json` makes it a single JSON object so a calling agent can parse
it instead of scraping prose.

## Exit codes (CLI mode)

| Code | Meaning |
|---|---|
| `0` | Success |
| `1` | Usage or configuration error (missing URL/token, bad arguments) |
| `2` | Local file problem (missing, unreadable, a directory) |
| `3` | Server refusal — any `4xx`; the JSON `code`/`message` is printed |
| `4` | Transport failure after retries (connection, DNS, TLS, `5xx`) |
| `5` | Integrity mismatch — what landed is not what was sent |

Distinct codes so a shell-driving agent can branch without parsing prose.

---

# Part 2 — the server side: verification, delivery, discoverability

Three small additions to `app/`. None of them change containment; none of them
add a way to name a path outside the vault.

## 2a. `X-Expect-Bytes` / `X-Expect-Sha256` on `PUT`/`POST /v1/content/{path}`

Today an interrupted upload is *already* safe — `_write_body` streams into the
temp fd inside `VAULT.atomic_write`, and an exception mid-stream leaves the
context manager without ever calling `committer.commit(fd)`, so nothing is
renamed into place. What is missing is the case where the body arrives complete
and wrong: a truncated-then-closed proxy response, a file mutated under the
reader, a caller sending the wrong file. `write_file` already closes exactly this
hole for MCP (`expect_bytes` / `expect_sha256`); REST should have the same floor,
and the bridge needs it to make a verified round trip in one request.

In `app/main.py:354` `_write_body`:

- Parse optional `X-Expect-Bytes` (int) and `X-Expect-Sha256` (64 hex chars,
  case-insensitive) from the request headers. A malformed value is a
  `400 bad_request` naming the header — not a silent ignore, which would mean a
  typo'd header buys the caller nothing while looking like a guarantee.
- If `X-Expect-Bytes` is present, compare it to `Content-Length` *before*
  streaming when both are known, and refuse a mismatch with `400` immediately.
- Feed every streamed chunk into a `hashlib.sha256()` alongside the existing
  `os.write(fd, chunk)`, and compute the total unconditionally — it costs
  microseconds per MB and it is what lets the response carry a digest for every
  caller, not just verifying ones.
- After the stream and **before `committer.commit(fd)`**, compare against
  whichever expectations were sent. On mismatch raise
  `HTTPException(422, detail={"code": "integrity_mismatch", "message": …})`
  naming both the expected and received values, and let `atomic_write` clean up
  the temp file. Nothing is renamed; the destination is untouched. Use `422`, not
  `400`: the request was well-formed, its content failed verification, and a
  distinct code lets a client tell "you sent me garbage" from "you asked wrongly".
- Return `(size, sha256_hex)` instead of `size`, and thread it through both
  `put_content` and `post_content`.

Add `sha256: str | None = None` to `OkResponse` (`app/main.py:144`) and populate
it on every successful write. Purely additive — existing clients ignore a new
field.

Keep the existing `MAX_UPLOAD_BYTES` checks exactly where they are; verification
runs after them, never instead of them.

## 2b. `GET /v1/bridge.py` — delivery in one command

The bridge is worthless if getting it onto the agent's machine is a research
project. The server already has the file (`COPY bridge/ /srv/bridge/`, one new
Dockerfile line next to the existing `COPY app/ /srv/app/`), so it can hand it
out:

```python
@app.get("/v1/bridge.py", tags=["meta"], summary="Download the local upload bridge")
def bridge_script(_: ReadAuth) -> Response:
```

- Read auth, same as every other route. The content is open-source client code,
  but an unauthenticated route is a new unauthenticated surface, and `/health` is
  deliberately the only one.
- Serve the file bytes with `media_type="text/x-python"` and
  `Content-Disposition: attachment; filename="claude_files_bridge.py"`.
- Resolve the path as `/srv/bridge/claude_files_bridge.py` if it exists, else
  `Path(__file__).resolve().parents[1] / "bridge" / "claude_files_bridge.py"` so
  a source checkout (`uvicorn app.main:app` from the repo root, which is how the
  tests run) serves it too. **Fixed paths only — no request input reaches this
  lookup.** If neither exists, `503` with
  `{"code": "bridge_unavailable", "message": …}`; that is a packaging bug, and a
  clear 503 beats a 500 traceback.

Then the recipe from any shell, anywhere, is two lines:

```bash
curl -sf -H "Authorization: Bearer $CLAUDE_FILES_TOKEN" -o claude_files_bridge.py "$CLAUDE_FILES_URL/v1/bridge.py"
python3 claude_files_bridge.py upload ./brief.pdf briefs/2026/brief.pdf
```

## 2c. Tell the agent the bridge exists

Discoverability is half of this fix. The reporting agent proposed the right
design from first principles because nothing in the tool surface told it there
was a non-generated path. Fix that in the three places an agent actually looks:

1. **`get_folder_info` (`app/mcp_endpoint.py:348`)** gains a `bridge` block:
   ```python
   "bridge": {
       "available": True,
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
   }
   ```
2. **`write_file`'s oversize-base64 refusal (`app/mcp_endpoint.py:547`)** gains a
   first sentence naming the bridge, before the existing `begin_write` and REST
   suggestions. The refusal message is read at precisely the moment it is
   actionable, which makes it the highest-value string in this document.
3. **`GET /v1/info`** gains `"bridge_available": <bool>`, matching the
   `mcp_enabled` flag already there, so a human can confirm a deployment serves
   it without probing.

---

# Part 3 — documentation

- **`README.md`**: a `### Uploading binary files: the local bridge` subsection
  under `## MCP connector`, after "Large and binary writes". Lead with the
  constraint in one sentence (*the model's own output ceiling, not any server
  limit, is what caps an inline write*), then the two-line curl recipe, then the
  Claude Code MCP config, then one line each on `download_file` and
  `bridge_status`. Add `GET /v1/bridge.py` to the API table. Add
  `X-Expect-Bytes` / `X-Expect-Sha256` to the `PUT`/`POST` rows. State plainly
  that the bridge runs on the *agent's* machine and holds the token, so it
  should only be configured somewhere the token already belongs.
- **`AGENT.md`**: a `## Uploading a file you generated` section — the paste-ready
  brief is the thing handed to agents, and this is the whole point. Three lines:
  fetch the bridge, run it, never base64 a binary file inline.
- **`bridge/README.md`**: install, the four env vars, the three modes, a worked
  Claude Code `.mcp.json`, and the exit-code table.
- **`.env.example`**: nothing new (the bridge's variables live on the client),
  but add a comment under the MCP block pointing at `bridge/README.md`.
- **README "Configuration" table**: unchanged — deliberately note in the bridge
  section that the bridge is configured by *client-side* environment, which is
  why it is not in that table.
- **`docs/plan-mcp-large-writes.md`**: add one line to "Open questions" #2
  answering it from this incident — a real connector did *not* drive the chunked
  loop, because it never saw the tools (Part 0), and the follow-on answer is this
  document.

## Setting it up on each surface

Spell these out in `bridge/README.md`; an implementer should not have to derive
them.

**Claude Code (desktop, cloud session, IDE)** — `.mcp.json` in the project, or
`claude mcp add`:

```json
{
  "mcpServers": {
    "claude-files-bridge": {
      "command": "python3",
      "args": ["/absolute/path/to/claude_files_bridge.py", "serve"],
      "env": {
        "CLAUDE_FILES_URL": "https://files.example.com",
        "CLAUDE_FILES_TOKEN_FILE": "/absolute/path/to/claude-files.token"
      }
    }
  }
}
```

`CLAUDE_FILES_TOKEN_FILE` rather than `CLAUDE_FILES_TOKEN` because `.mcp.json`
is frequently committed.

**A surface with a shell but no local MCP** — the two-line curl recipe from 2b,
once per session.

**A surface with neither a shell nor a local process** (the claude.ai web app
with only the remote connector): the bridge cannot run there at all. That surface
keeps `begin_write`/`write_chunk`/`commit_write`, which is exactly why Part 0
matters and why this feature *supplements* the chunked path rather than
replacing it.

---

## Files to change

| File | Change |
|---|---|
| `bridge/claude_files_bridge.py` | **new** — the whole feature |
| `bridge/README.md` | **new** |
| `bridge/requirements.txt` | **new** — `mcp==1.12.4`, only for `serve` |
| `tests/bridge_tests.py` | **new** — see below |
| `app/main.py` | `version="1.1.0"`; `_write_body` hashes and verifies; `OkResponse.sha256`; `GET /v1/bridge.py`; `/v1/info` gains `bridge_available` |
| `app/mcp_endpoint.py` | `get_folder_info` gains the `bridge` block; `write_file`'s oversize refusal names the bridge |
| `Dockerfile` | `COPY bridge/ /srv/bridge/` next to the existing `COPY app/`; include it in the `compileall` line |
| `docker-compose.yml` | pins → `:1.1.0` |
| `.github/workflows/ci.yml` | `py_compile` gains `bridge/*.py`; new job or step for `tests/bridge_tests.py` |
| `tests/run_tests.sh` | invoke `tests/bridge_tests.py` inside `run_mode` |
| `README.md`, `AGENT.md`, `.env.example` | as Part 3 |
| `docs/plan-mcp-large-writes.md` | one line answering open question 2 |

`app/safepath.py` is **not** in this list. If an implementation finds itself
editing it, something has gone wrong — re-read "Why `local_path` cannot work".

---

## Tests

New file `tests/bridge_tests.py`, in the style of `tests/escape_suite.py`
(`Results` class, `PASS`/`FAIL` lines, non-zero exit on any failure), driven from
`run_tests.sh` inside `run_mode` so it runs against **both** container
configurations alongside the existing suites. It imports the bridge module
directly (`sys.path.insert` on `bridge/`) and calls `upload` / `download` /
`check` — **no `mcp` package required**, which is the reason the core functions
take no `Context`.

Environment it reads, matching the existing suites: `BASE_URL`, `API_TOKEN`,
`API_TOKEN_READONLY`, `FIXTURE_REPO`, `MODE`. It sets `CLAUDE_FILES_URL` /
`CLAUDE_FILES_TOKEN` itself from those.

1. **Round trip, binary.** Generate 40 KB of `os.urandom`, write it to a temp
   file, `upload` it, then read it back over REST and assert byte-identity and
   matching sha256. 40 KB is deliberately ~5× `MCP_BASE64_LIMIT_BYTES` — a size
   `write_file` refuses outright.
2. **Round trip, large.** Size it from the server, not from a literal: read
   `max_upload_bytes` from `GET /v1/info` and upload `int(cap * 0.75)` random
   bytes. The suite runs the container with `MAX_UPLOAD_BYTES=2097152`
   (`tests/run_tests.sh:97`), so that is ~1.5 MB there and it rescales if that
   override ever moves — never hardcode a size that an edit to line 97 turns
   into a silent `413`.
3. **No memory blow-up.** Wrap the test-2 upload in `tracemalloc` and assert the
   peak allocation stays under 4 MiB — comfortably above the 1 MiB hashing block
   and far below what a whole-file `f.read()` would cost. This catches the single
   most likely way this file rots: someone "simplifying" the streamed body into a
   `read()`.
4. **`sha256` in the response.** Assert `upload`'s result has
   `verified is True` and a `sha256` matching a locally computed digest.
5. **`X-Expect-Sha256` mismatch refuses, and nothing lands.** Send a deliberately
   wrong digest at the REST layer directly; assert `422 integrity_mismatch`, and
   assert the destination path still does not exist (or is unchanged if it did).
   This is the Part 2a floor and the direct descendant of the previous incident.
6. **`X-Expect-Bytes` mismatch refuses** the same way.
7. **Malformed `X-Expect-Sha256`** (`"not-a-hash"`) → `400`, naming the header.
   Not a silent ignore.
8. **Overwrite semantics.** `overwrite=False` onto an existing path → refusal
   carrying the server's `409 exists`, and the original bytes are untouched.
9. **`parents=True`** creates the chain; `parents=False` into a missing directory
   → `409 no_parent` naming the missing folder.
10. **Read-only token.** With `API_TOKEN_READONLY`, `upload` fails with the
    server's `403 readonly` and `download` succeeds.
11. **Traversal is refused, and nothing escapes.** `upload` to `../escape.txt`,
    `..%2Fescape.txt`, `/etc/cron.d/x`, and a path with a NUL. Assert — in the
    style of the existing suites — not merely that an error came back, but that
    the file outside `FIXTURE_REPO/shared` is still byte-identical. Some of these
    are refused client-side (step 2 of "Remote path handling") and some
    server-side; the test must not care which, only that nothing escaped.
12. **Local-side refusals.** A missing file → exit code 2; a directory → exit
    code 2 with "is a directory"; a file over `max_upload_bytes` → refused
    locally, and assert *no request reached the server* (check the byte count
    didn't move / the server log has no entry for it).
13. **Download is atomic.** Kill a download mid-stream (patch the block reader to
    raise after one block) and assert no file at `local_path`, and that the
    `.part` file is cleaned up.
14. **`check` on a broken config.** Unset `CLAUDE_FILES_TOKEN` → a result naming
    the missing variable, exit code 1, and no traceback.
15. **`GET /v1/bridge.py`** returns Python source starting with a shebang, is
    refused without a token (`401`), and is *not* reachable through the vault
    (assert `GET /v1/content/bridge.py` is a normal `404` — the script is served
    from the image, never from the shared folder).
16. **`/v1/info` reports `bridge_available: true`**, and `get_folder_info`'s
    `bridge.note` is present and non-empty.
17. **Optional, skipped when `mcp` is absent:** spawn
    `python3 bridge/claude_files_bridge.py serve`, do a real stdio
    `initialize` + `tools/list` handshake, assert exactly the three tool names,
    and assert **nothing but JSON-RPC frames appeared on stdout** — the stdout
    discipline rule, tested rather than trusted.

CI: add `bridge/*.py` to the `py_compile` line in `quick-checks`. Test 17 needs
`pip install -r bridge/requirements.txt` on the runner; if that is unwelcome in
the containment job, give it its own small job that installs only the bridge
requirements.

---

## What this does not fix

Say these plainly in `bridge/README.md` rather than letting them be discovered.

- **A sandbox with no outbound network to the server.** If the agent's
  environment cannot reach `CLAUDE_FILES_URL` at all, no client-side design
  helps; the bytes have nowhere to go. `bridge_status` / `check` exists to make
  that diagnosis take one call instead of an afternoon.
- **A surface with no filesystem and no shell.** The bridge is code that must
  run somewhere. Where nothing can run, `begin_write`/`write_chunk` remain the
  only path — slower, ceiling-bound, but correct once Part 0 ships.
- **The token reaching a new machine.** The bridge holds a credential wherever it
  runs. That is not a new risk class (any agent using the connector already holds
  the token) but it is a new *location*, and `CLAUDE_FILES_TOKEN_FILE` plus "use
  `API_TOKEN_READONLY` if you only need downloads" are the mitigations. Say so in
  the README next to the setup instructions, not in a footnote.
- **Recovering an overwritten file.** Still out of scope, still noted in
  `docs/plan-mcp-large-writes.md`, "Separate issue". The bridge makes a *corrupt*
  overwrite essentially impossible; it does nothing about a *deliberate* one.

---

## Considered and not chosen

- **`local_path` on the remote `write_file`.** The literal reading of the
  request. Impossible (the server is on a different machine) and, if implemented
  anyway, an arbitrary-file-read hole in the one service whose reason to exist is
  containment. Covered above; recorded here so nobody re-proposes it from the
  title alone.
- **`write_from_url` (server fetches the bytes).** Rejected in
  `docs/plan-mcp-large-writes.md` for SSRF and link-local metadata exposure, and
  the reasoning is unchanged. It also does not solve this case: the agent's file
  is on local disk, not at a URL the server could reach.
- **Bigger `MCP_BASE64_LIMIT_BYTES`.** The reporting agent dismantled this
  correctly — the binding limit is on the model's side and the server cannot see
  it. Raising the number only moves where the silent failure would have happened,
  which is the exact bug `009633b` fixed.
- **Chunked writes alone (the status quo).** Works, ships already, and remains
  the fallback for surfaces that can run nothing. But it is ~12 round trips for a
  30 KB PDF, every byte still generated, and the previous document's own open
  question 2 — "does a real connector tolerate a ~10-call upload loop?" — is now
  answered less encouragingly than hoped: the one agent that hit this reached for
  a path-based primitive instead.
- **A bridge mirroring all 13 connector tools.** Two implementations of one
  surface, drifting. The bridge covers only what the connector structurally
  cannot do.
- **Signed upload tickets (short-lived URL, agent `PUT`s directly).** Dismissed
  in the previous document as only working where the agent can make arbitrary
  HTTP calls. Note that the bridge's CLI mode has the same prerequisite — but it
  ships the correctness (streaming, hashing, retries, verification, path quoting)
  with it, instead of asking a model to reinvent it in `curl` every time.
- **Packaging the bridge to PyPI.** Real convenience, real supply-chain and
  release-process weight, and it needs network access to a package index that the
  target environments may not have. `GET /v1/bridge.py` gets the same file onto
  the machine using the connection the agent is already required to have.

---

## Open questions

1. **Should `upload_file` accept a list of local paths?** `device_commit_files`
   does, and an agent that generated a PDF plus a log has two uploads. Trivial to
   add later; left out now because a single-file signature keeps per-file error
   reporting unambiguous, which matters more on the first iteration than
   batching.
2. **Should the bridge verify the server's TLS certificate against a pinned
   fingerprint?** Overkill for a tunnel already fronted by Cloudflare, but the
   bridge is the first component that holds the rw token *outside* the operator's
   own machines. Revisit if the bridge ends up on shared infrastructure.
3. **Does `GET /v1/bridge.py` belong behind read auth or no auth?** Chosen: read
   auth, for consistency with "only `/health` is unauthenticated". The cost is
   that the recipe needs a header, which is one more thing for an agent to get
   right. If real use shows agents fumbling it, unauthenticated is defensible —
   the file is public source — but change it deliberately, not by accident.
