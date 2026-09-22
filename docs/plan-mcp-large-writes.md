# Plan: fix silent truncation on MCP writes, and add chunked write sessions

**Status: specified, not implemented. Ready to hand to an implementer.**

The capability option was chosen: **Option B, chunked write sessions.** This
document is now an implementation brief for that plus the integrity floor it
depends on. The options that were weighed and not chosen are kept at the end,
condensed, so the reasoning survives.

Follows [`docs/plan-mcp-connector.md`](plan-mcp-connector.md), which is
implemented and shipped (`app/mcp_endpoint.py`).

**Build in two commits, in this order:** Part 1 (the floor) is independently
correct, testable, and fixes the actual defect. Part 2 depends on it — chunking
without integrity verification would just move the silent corruption to a
chunk boundary.

---

## The incident

An agent using the live connector was asked to re-upload an already-archived
~28 KB PDF. Reported verbatim:

> Re-uploading today's already-archived PDF required me to pass the full ~28KB
> file as one inline base64 string, and I hit a hard ceiling on how much text I
> can generate in a single tool call — every attempt truncated at the same
> ~6.5KB mark, **silently producing a corrupt, invalid PDF** at
> `daily-briefs/2026/2026-09-22-daily-brief.pdf`. I caught this, and rather
> than leave a broken file sitting there, I deleted it.

The agent did everything right: called `get_folder_info` first as instructed,
worked inside the budget that tool reported, and got back a success result.
Treat this as a server defect, not a usage error.

---

## Root cause: three separate defects

### 1. Truncated base64 decodes silently — the corruption itself

`write_file` (`app/mcp_endpoint.py:426-430`) validates base64 like this:

```python
data = base64.b64decode(content, validate=True)
```

`validate=True` only rejects characters outside the base64 alphabet. **It
cannot detect that the string is short.** A prefix of a valid base64 string is
itself valid base64 whenever its length is a multiple of 4. Measured on a
28,160-byte input:

```
file bytes 28160 -> base64 chars 37548
  cut=6500  len%4=0 -> DECODED SILENTLY into 4875 bytes
  cut=6501  len%4=1 -> raised Error: number of data characters cannot be 1 more than a multiple of 4
  cut=6502  len%4=2 -> raised Error: Incorrect padding
  cut=6503  len%4=3 -> raised Error: Incorrect padding
```

A truncated upload therefore succeeds silently **1 time in 4** — and *every*
time, if the ceiling is a deterministic output-token budget that cuts at the
same offset on every retry. That is exactly the "same ~6.5KB mark every
attempt" the agent described; 6500 base64 characters decodes to 4875 bytes.

Nothing in the call carried an expected length or checksum, so the server had
no signal to check against. **This is the defect. It is independent of
chunking, and Part 1 fixes it.**

### 2. The advertised budgets are wrong by roughly 200×

`get_folder_info` is the tool the agent was told to call first to learn its
limits. Queried against the live server:

```json
{"mcp_inline_limit_chars": 100000, "mcp_base64_limit_bytes": 1048576, ...}
```

`MCP_BASE64_LIMIT_BYTES` defaults to 1 MiB (`app/config.py:49`, `:83`). The
real ceiling is whatever the caller can emit in one tool call — observed at
roughly **5 KB of decoded bytes**. That number came from
`docs/plan-mcp-connector.md`, and it was wrong when written: it was picked as a
*context-cost* budget without asking what a model can actually generate.

**The ceiling is not a constant** — it varies by model, by surface, and by how
much context is already spent. Nothing should hardcode it as truth. That is why
Part 1 detects the failure rather than predicting it, and why the advertised
number below is explicitly labelled advisory.

### 3. The same mistake on the read side, not yet triggered

`read_file` with `encoding="base64"` (`app/mcp_endpoint.py:348-360`) has **no
truncation guard at all**. The text path has one — over budget returns
`{preview, bytes, truncated, note}` — but the base64 path either returns the
whole thing or raises. `read_file("x.pdf", encoding="base64")` on a 900 KB file
would hand the caller 1.2 MB of base64 in one tool result. Same wrong
assumption, opposite direction. Fix it in the same pass.

---

# Part 1 — the integrity floor

**Required. Part 2 is unsafe without it.**

### `write_file` gains verification arguments

```python
def write_file(path, content, ctx, encoding="utf-8", parents=True,
               overwrite=True,
               expect_bytes: int | None = None,
               expect_sha256: str | None = None) -> dict:
```

- After decoding, compare `len(data)` against `expect_bytes` and
  `hashlib.sha256(data).hexdigest()` against `expect_sha256`.
- **Verify before `VAULT.atomic_write` is entered.** On mismatch nothing is
  created, so there is no corrupt file to notice and delete. This ordering is
  the whole point — get it wrong and the incident repeats with a better error
  message.
- The error must name truncation as the likely cause, because that is
  overwhelmingly what a short write means:

  > `Received 4,875 bytes but expect_bytes said 28,160 — the content was almost
  > certainly truncated in transit. Nothing was written. For a file this size,
  > use begin_write/write_chunk/commit_write instead.`

- **Require at least one of the two for base64 writes over 1 KB decoded.** A
  caller generating novel binary content cannot produce a hash for it, so both
  stay optional in general — but a base64 write is nearly always a copy of
  bytes that came from somewhere, and somewhere can supply a length.

### `read_file` returns integrity metadata on every path

Add `sha256` and `bytes` to every successful `read_file` result — base64, text,
and truncated-preview alike. This is what makes verification free for the copy
case: the caller is not computing a hash, it is forwarding two values it was
handed. Truncate the forwarded content and the numbers stop matching.

Also add the missing base64 truncation guard from defect 3.

### Correct the budgets

| Setting | Now | Change to |
|---|---|---|
| `MCP_BASE64_LIMIT_BYTES` (`app/config.py:49`, `:83`) | 1 MiB | **8 KB** — a single-shot ceiling, above which `write_file` refuses and names `begin_write` |
| `MCP_INLINE_LIMIT_CHARS` | 100 000 | unchanged — it governs reads, where the budget is a context cost and 100k is defensible |

`get_folder_info` (`app/mcp_endpoint.py:260`) must report these **as advisory**,
with a note that the real write ceiling is the caller's own output budget,
which the server cannot see, and that `begin_write` exists for anything larger.

---

# Part 2 — chunked write sessions

## Tool surface

```python
begin_write(path, ctx, encoding="utf-8", overwrite=True, parents=True)
    -> {"upload_id": str, "path": str, "chunk_advisory_bytes": int,
        "expires_at": str, "note": str}

write_chunk(upload_id, seq, content, ctx)
    -> {"seq": int, "chunks_received": int, "bytes_received": int}

commit_write(upload_id, ctx, expect_bytes=None, expect_sha256=None)
    -> {"ok": true, "path": str, "size": int, "sha256": str, "created": true}

abort_write(upload_id, ctx)
    -> {"ok": true, "aborted": true}
```

All four require a read-write token (`_require_write(ctx)`,
`app/mcp_endpoint.py:232`) — including `abort_write`.

`commit_write` **requires at least one of `expect_bytes` / `expect_sha256`**.
This is stricter than `write_file`'s over-1 KB rule, deliberately: anything
worth chunking is worth verifying, and by the time a caller has made six tool
calls it has no excuse for not knowing how many bytes it meant to send. Refuse
with a message saying so rather than committing unverified.

## Why this does not break the atomicity guarantee

`docs/plan-mcp-connector.md` rejected `append_file` on the grounds that atomic
replace is a documented promise (`README.md`: "a reader never sees a
half-written file"), implemented as write-temp-then-`rename`
(`app/safepath.py:442-453`).

**A chunked session is that same mechanism, held open across several calls.**
Chunks land in the same `.upload-<hex>.tmp` file; `commit_write` performs the
same single `os.rename`. A reader still never sees a partial file, and an
abandoned session never becomes one. Appending to a *live* file would break the
promise; this does not. Say so in the README when documenting it, because
"chunked upload" reads like the opposite of "atomic" and it is worth
disarming that.

## Holding the write open — the mechanical crux

`Vault.atomic_write` (`app/safepath.py:410`) is a `@contextmanager` whose
`finally` closes the fd **and** calls `committer.cleanup()`, which unlinks the
temp file. Nested inside it, `_parent_fd` → `_dir_fd` owns `dir_fd` and closes
it on exit. `Committer` holds `dir_fd` but does not own it. So the whole chain
has to stay entered for the lifetime of the session.

Drive it with a `contextlib.ExitStack` stored on the session:

```python
stack = contextlib.ExitStack()
fd, committer = stack.enter_context(
    VAULT.atomic_write(parts, overwrite=overwrite, create_parents=parents)
)
# ... later, from a different request:
committer.commit(fd)     # fsync + rename
stack.close()            # closes fds; cleanup() is a no-op once committed
```

On abort or expiry, `stack.close()` alone does the right thing: `cleanup()`
unlinks the temp file and both fds close.

**This requires no changes to `app/safepath.py`.** That is a property worth
preserving deliberately — the containment core is the audited part of this
repo, and a feature that needs none of it rewritten is a feature that cannot
regress it. If the implementation starts wanting to modify `safepath.py`, stop
and reconsider the approach.

Two consequences to handle:

- **These are sync generator context managers.** FastMCP runs sync `def` tools
  in a threadpool, so a session is entered on one thread and closed on another.
  That is fine for plain generators and fds, but **do not convert these tools to
  `async def`** — an async context manager entered in one event-loop task and
  exited in another is a different and worse problem.
- **One lock per session** (`threading.Lock`). Two `write_chunk` calls racing on
  the same `upload_id` would interleave `os.write` on one fd. Hold the lock for
  the whole chunk operation; a second concurrent call either waits or is
  refused — prefer refusing with a clear "a chunk is already in flight for this
  upload" over silently serialising, since concurrent chunks mean the caller has
  a bug.

## Session state

An in-process dict in `app/mcp_endpoint.py`, keyed by `upload_id`
(`secrets.token_urlsafe(16)`):

| Field | Purpose |
|---|---|
| `stack`, `fd`, `committer` | the held-open write, above |
| `parts` | the validated path components, for messages |
| `encoding` | fixed at `begin_write`; chunks may not change it |
| `next_seq` | the only sequence number that will be accepted |
| `bytes_received`, `chunks_received` | reported back; checked at commit |
| `hasher` | running `hashlib.sha256`, updated per chunk |
| `token_digest` | binds the session to the credential that opened it |
| `created_at`, `last_activity_at` | expiry |
| `lock` | above |

**The `upload_id` is the continuity, not the MCP transport.** This works
unchanged under `stateless_http=True` (chosen in the connector plan) because
nothing depends on the transport keeping a session — each call is an
independent HTTP request carrying the id. Do not reach for MCP session state
here.

**Single process only.** The image runs `uvicorn app.main:app` with no
`--workers` (`Dockerfile:66-69`), so one process holds all sessions. Adding
workers would route chunks to processes that have never heard of the
`upload_id`. Put a comment saying so at the dict's definition — naming the
failure, per this repo's comment convention — and do not attempt to make it
multi-process.

## Ordering: make gaps and duplicates impossible

**`seq` must equal the session's `next_seq`, starting at 0.** Not "greater
than", not "not yet seen" — exactly equal. Anything else is refused:

> `Expected seq=3 for this upload (3 chunks received so far), got seq=5. Chunks
> must arrive in order with no gaps. Resend seq=3.`

This makes gaps, reordering and duplicates structurally impossible rather than
detected-if-you-remembered-to-check. A retried chunk after a failed call is the
one case a caller will actually hit, and equality handles it: the failed chunk
never incremented `next_seq`, so resending the same `seq` is correct and works.

Chunked writes that reintroduce this class of bug through sloppy ordering would
be an embarrassing way to fix this particular incident. The commit-time hash is
the backstop, but ordering should not rely on it.

## Base64 chunk boundaries — verified, and easy to get wrong

Base64 may only be split **at multiples of 4 characters.** Four base64
characters encode exactly 3 bytes, so a slice at a multiple of 4 is itself a
standalone-decodable base64 string with no padding; only the final chunk
carries `=`. Verified:

```
bytes 28164 -> b64 chars 37552
slicing at 4096: chunks 10, lens [4096, 4096, 4096, ... 688]
each chunk decoded standalone, concatenated -> identical to source, sha matches
slicing at 4095 -> Error: Incorrect padding
```

So `write_chunk` **rejects any non-final chunk whose base64 length is not a
multiple of 4**, with a message saying to slice on a 4-character boundary. This
is worth doing for two reasons: it is what makes per-chunk streaming decode
possible at all, and it catches a mid-chunk truncation immediately in 3 cases
out of 4 rather than deferring every failure to the commit hash.

A chunk is decoded and written on arrival — never buffered in memory to be
decoded at commit. The point of chunking is that the whole file is never
resident anywhere at once.

For `encoding="utf-8"`, chunks are `.encode("utf-8")`ed and concatenated;
there is no boundary rule, but note that a caller must not split a multi-byte
character across chunks. Say so in the tool description.

## Expiry, limits, and cleanup

| Setting | Default | Meaning |
|---|---|---|
| `MCP_UPLOAD_SESSION_TTL_SECONDS` | `900` | Idle timeout, measured from `last_activity_at` |
| `MCP_MAX_CONCURRENT_UPLOADS` | `8` | Refuse `begin_write` beyond this, with a message naming `abort_write` |
| `MCP_UPLOAD_CHUNK_ADVISORY_BYTES` | `3072` | Reported by `begin_write` and `get_folder_info`; advisory, not enforced |

- **Reap lazily**, at the top of `begin_write` and `write_chunk` — sweep expired
  sessions and `stack.close()` them. No background task, nothing new in the
  lifespan. A daemon that exists only to clean up after a rarely-used feature is
  more moving parts than the feature.
- `MAX_UPLOAD_BYTES` (`app/config.py`) applies to the **cumulative** total.
  Check it on every chunk, not just at commit — otherwise a caller can stream
  256 MiB onto the disk before being told no.
- **Abandoned sessions already fail safe.** `Vault.sweep_temp_files`
  (`app/safepath.py:179`) removes leftover `.upload-*.tmp` at startup, so a
  crash mid-session leaves no debris beyond the next restart. The concurrency
  cap bounds open fds in the meantime.
- `abort_write` on an unknown or already-finished `upload_id` returns
  `{"ok": true, "aborted": false}` rather than raising. Cleanup should be safe
  to call twice; a caller tidying up after an error should not get a second
  error for its trouble.

## Auth binding

Store a digest of the presented credential on the session at `begin_write`, and
refuse chunks or commits presenting a different one. There are only two tokens
in play (`API_TOKEN`, `API_TOKEN_READONLY`) and the read-only one cannot open a
session at all, so the practical exposure is small — but an `upload_id` should
not be a bearer credential of its own, and a session holding an open write fd
to a path chosen by one caller should not be completable by another.

Use `hmac.compare_digest` on the digests, consistent with `_check_token`
(`app/main.py`).

## Containment across a session

The path is validated once, at `begin_write`, through `VAULT.components()` and
`_parent_fd` — the same one-component-at-a-time `openat` walk with `O_NOFOLLOW`
as every other write.

**No re-validation is needed at commit, and this is not a gap.** `dir_fd` is
pinned as an open file descriptor for the whole session, and `Committer.commit`
renames with `src_dir_fd=dst_dir_fd=dir_fd` (`app/safepath.py:574`). A
descriptor cannot be swapped for a symlink after the fact, so the destination
directory at commit is byte-for-byte the one that passed validation at begin.
That is the same property that makes the existing single-shot write immune to
TOCTOU, extended over a longer window. Write it down in the module docstring —
a reviewer will ask.

One behavioural note to document rather than fix: `overwrite=False` is checked
at `begin_write`. A file created at that path *during* the session will be
overwritten at commit. Re-checking at commit would not close the race either
(the window just moves), and failing a fully-uploaded 200 KB file at the last
step is worse than the race. Document it in the tool description.

## Interaction with the autocommit sidecar

Already handled, and worth confirming rather than rediscovering: the sidecar
excludes `.upload-*.tmp` by pathspec (`README.md`, "In-flight uploads"), so a
chunked upload in progress is never committed to git half-written. The file
enters the repo only at the `rename`, as a complete file. This is the existing
in-flight-upload protection doing exactly the job it was built for.

## Errors

Every error goes through `_guard` (`app/mcp_endpoint.py:213`) so denials stay
logged — that is the observability property the connector plan established and
it must not be bypassed by the new tools. Messages should name the next action:

| Condition | Message names |
|---|---|
| Wrong `seq` | the expected `seq`, and to resend it |
| Non-multiple-of-4 base64 in a non-final chunk | the 4-character boundary rule |
| Unknown / expired `upload_id` | that it expired and to start again with `begin_write` |
| Cumulative bytes over `MAX_UPLOAD_BYTES` | the limit and the bytes received so far |
| Commit with neither `expect_*` | that one is required, and why |
| Commit hash/length mismatch | both values, and that nothing was written |
| Too many concurrent sessions | the cap and `abort_write` |

## Making the tools actually get used

A capability the model does not know to reach for is not a fix. Three places
have to say the same thing:

1. **`write_file`'s description** — over the single-shot cap, refuse and name
   `begin_write` explicitly. The refusal is the discovery path for a caller that
   did the naive thing.
2. **`get_folder_info`** — report `chunk_advisory_bytes` and mention the chunked
   tools alongside the single-shot budgets.
3. **`FastMCP(instructions=...)`** and `AGENT.md` — one sentence: files over a
   few KB go through `begin_write`/`write_chunk`/`commit_write`, and
   `commit_write` needs the byte count or hash.

`begin_write`'s own result carries `chunk_advisory_bytes` and a `note` with the
loop shape, so a caller that has started does not need to guess the next step.

## No REST equivalent, deliberately

`docs/plan-mcp-connector.md` set the principle that REST and MCP are one
implementation. This is a deliberate exception: REST already streams an
arbitrarily large raw body through `PUT /v1/content/{path}`
(`app/main.py:345`). It has no output-token ceiling, so it has no
problem to solve. Adding a chunked REST API would be inventing a second way to
do something the first way already does well. Note this in the README so the
asymmetry reads as a decision.

---

## Files to change

| File | Change |
|---|---|
| `app/mcp_endpoint.py:401` | `write_file`: `expect_bytes`/`expect_sha256`, verified **before** `atomic_write`; refuse over the new cap naming `begin_write` |
| `app/mcp_endpoint.py:326` | `read_file`: return `sha256`+`bytes` on every path; add the missing base64 truncation guard |
| `app/mcp_endpoint.py:260` | `get_folder_info`: advisory budgets + chunk advisory + mention of the chunked tools |
| `app/mcp_endpoint.py` | new: session registry, reaper, and the four tools |
| `app/config.py:49`, `:83` | `MCP_BASE64_LIMIT_BYTES` 1 MiB → 8 KB; add the three new settings |
| `app/safepath.py` | **none** — see "Holding the write open" |
| `tests/mcp_escape_suite.py` | see below |
| `README.md` | the chunked tools, the atomicity argument, why there is no REST equivalent, corrected budgets |
| `AGENT.md`, `.env.example`, `unraid/claude-files.xml`, `docker-compose.yml` | new settings and the one-sentence usage note |
| `docs/plan-mcp-connector.md` | correct the 1 MiB figure and the reasoning that produced it — that document is why the number was wrong |
| this file | mark implemented, record what differed |

## Tests

Same style as the existing suites: standalone script, `Results` accumulator,
non-zero exit. Containment tests run in **both** container modes (`isolated`
and `app-only`) via `tests/run_tests.sh`.

**Part 1**

1. A base64 `write_file` truncated to a multiple of 4, with the *original*
   `expect_bytes`, is refused **and no file is created**. The second half is the
   point — this is the regression test for the incident.
2. The same truncation with neither `expect_*`, over the 1 KB threshold, is
   refused rather than silently writing short.
3. Round trip: `read_file` → `write_file` forwarding the returned
   `sha256`/`bytes` succeeds and the destination is byte-identical.
4. `read_file` on a binary file over the base64 cap is refused with the REST
   pointer, not returned whole.
5. `get_folder_info`'s advertised numbers and the values actually enforced come
   from the same config, asserted together so they cannot drift again — defect 2
   was exactly that drift.

**Part 2**

6. Happy path: a file several chunks long round-trips byte-identical, with a
   matching `sha256`, in both encodings.
7. Ordering: `seq` skipped, repeated, and out of order are each refused; a
   resend after a failed chunk succeeds.
8. A non-final base64 chunk not a multiple of 4 is refused; the final chunk may
   be any length.
9. Commit with a wrong `expect_sha256`, and with a wrong `expect_bytes`, each
   refuse **and leave no file at the destination**.
10. Commit with neither is refused.
11. Abandonment: a session left open past its TTL is reaped, its
    `.upload-*.tmp` is gone, the destination does not exist, and the
    `upload_id` is unknown afterwards. Assert the temp file's absence directly
    on disk, not just the tool's answer.
12. `abort_write` mid-session leaves no temp file and no destination; calling it
    twice, and on an unknown id, both succeed quietly.
13. Cumulative `MAX_UPLOAD_BYTES` is enforced *during* chunking, not only at
    commit.
14. `MCP_MAX_CONCURRENT_UPLOADS` is enforced, and the message names
    `abort_write`.
15. Read-only token: all four tools refused, including `abort_write`.
16. A session opened with one token cannot be chunked or committed with
    another.
17. **Containment**, both modes: `begin_write` against every hostile path in the
    existing escape matrix — `..`, absolute, NUL, planted symlinks, hard links —
    is refused at `begin_write`, and the canary never appears. A session is
    never opened against a path that single-shot `write_file` would reject.
18. Atomicity: the destination does not exist at any point before `commit_write`
    returns. Poll it between chunks, or stat it directly.
19. Denials from the new tools appear in `docker logs` as `denied MCP …` —
    `_guard` is not bypassed.

## Considered and not chosen

Kept so the reasoning survives; see the git history of this file for the full
comparison.

- **A — `copy_file`** (server-side copy, ~30 lines from existing `Vault`
  primitives, no size ceiling, no new attack surface). Not chosen, but worth
  being clear-eyed about one thing: **the reported incident was a copy.** The
  bytes were already in the folder and the model was made into a courier for
  them. Under Option B that task becomes ~10 tool calls that will work; under A
  it would have been one call that never touches the model's output budget at
  all. B is the more general capability and that is why it was chosen — A
  remains a cheap, non-conflicting follow-on if re-archiving existing files
  turns out to be routine. Recorded as open question 3.
- **C — `write_from_url`.** No ceiling at all, but gives outbound network to a
  container whose premise is containment: SSRF, link-local metadata endpoints,
  and a hardening list (host allowlist, no redirects, DNS re-resolution at
  connect, streaming byte cap) that is most of the work. Wait for a requirement
  that names it.
- **D — corrected limits only.** Ship Part 1, document that bigger files go over
  REST. Turns on whether the agents using this have a shell; browser-based ones
  do not. Part 1 ships regardless, so this is the floor of every option.
- **E — upload tickets.** Short-lived signed URL, agent `PUT`s over REST. Only
  works where the agent can make arbitrary HTTP calls — which is exactly the
  case that did not need the connector. Dismissed.

## Separate issue: the deleted file is not recoverable from here

The agent deleted the corrupt PDF and had no tool to check the good version
back out of git. Real gap, but note that **with Part 1 in place the situation
does not arise** — the corrupt file is never created, so the original is never
overwritten.

The constraint is structural: the API container bind-mounts only the shared
subfolder and refuses `.git` as a path component (`DENIED_NAMES`, confirmed
live). Only the `autocommit/` sidecar mounts the repo root. Options, in
preference order: (1) document that recovery is a human `git checkout` and tell
agents to report and stop rather than improvise; (2) a read-only history
surface on the sidecar (`list_versions`/`restore_version`); (3) give the API
container the repo root — no, that is the exact mount this project exists to
avoid.

Worth a line in `delete_file`'s description either way: when a write looks
wrong, leaving the bad file and saying so beats deleting it, since the sidecar
pushes the deletion too. The agent's instinct was reasonable, but "stop and
report" beats "clean up" when the recovery path is unclear.

## Open questions

1. **Is 3 KB the right chunk advisory, and 8 KB the right single-shot cap?**
   Both are extrapolated from one data point (~4,875 bytes landed, at an unknown
   fraction of a context window). Measure across the surfaces actually in use
   and adjust the defaults. Part 1's verification is what makes being wrong
   about this survivable rather than corrupting.
2. **Does a real connector tolerate a ~10-call upload loop?** The mechanism is
   sound; whether a model reliably *drives* it to completion without giving up
   or losing the `upload_id` mid-sequence is a question only live use answers.
   If it proves flaky, the fix is a better `note` in `begin_write`'s result, not
   a bigger chunk size.
3. **Add `copy_file` as a follow-on?** It is ~30 lines and would have made the
   incident a non-event. Revisit once Option B is in and there is evidence about
   which case is actually common.
