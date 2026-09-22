# Plan: silent truncation on MCP writes, and the options for fixing it

**Status: proposed. Section "The floor" should happen regardless; the four
options after it are alternatives — pick one.** Follows
[`docs/plan-mcp-connector.md`](plan-mcp-connector.md), which is implemented and
shipped (`app/mcp_endpoint.py`).

## The incident

An agent using the live connector was asked to re-upload an already-archived
~28 KB PDF. Reported verbatim:

> Re-uploading today's already-archived PDF required me to pass the full ~28KB
> file as one inline base64 string, and I hit a hard ceiling on how much text I
> can generate in a single tool call — every attempt truncated at the same
> ~6.5KB mark, **silently producing a corrupt, invalid PDF** at
> `daily-briefs/2026/2026-09-22-daily-brief.pdf`. I caught this, and rather
> than leave a broken file sitting there, I deleted it.

`get_folder_info`, `list_files`, `stat_file`, `read_file` all worked. The agent
caught the corruption itself, by inspection, and cleaned up. Nothing in the
server told it anything was wrong — `write_file` returned `{"ok": true,
"size": 4875, "created": true}` or similar, which is a *successful* result.

This is worth treating as a real defect and not a usage error. The agent did
everything right: it called `get_folder_info` first as instructed, worked
inside the budget that tool reported, and got back a success.

---

## Root cause: three separate defects

### 1. Truncated base64 decodes silently — the corruption itself

`write_file` (`app/mcp_endpoint.py:426-430`) validates base64 like this:

```python
data = base64.b64decode(content, validate=True)
```

`validate=True` only rejects characters outside the base64 alphabet. **It does
not, and cannot, detect that the string is short.** Truncation is not a
corruption of the encoding — a prefix of a valid base64 string is itself a
valid base64 string whenever its length is a multiple of 4. Measured on a
28,160-byte input:

```
file bytes 28160 -> base64 chars 37548
  cut=6500  len%4=0 -> DECODED SILENTLY into 4875 bytes
  cut=6501  len%4=1 -> raised Error: number of data characters cannot be 1 more than a multiple of 4
  cut=6502  len%4=2 -> raised Error: Incorrect padding
  cut=6503  len%4=3 -> raised Error: Incorrect padding
```

So a truncated upload has a **1-in-4 chance of landing on a silent success**,
and — this is the part that made it reproduce every time — if the ceiling is a
deterministic output-token budget, the cut lands at the *same* character count
on every retry. A cut that is a multiple of 4 is therefore silent on every
attempt, which is exactly the "every attempt truncated at the same ~6.5KB mark"
the agent described. 6500 base64 characters decodes to 4875 bytes, consistent
with the report.

**The server has no way to know.** Nothing in the call carries the expected
length or a checksum, so there is no signal to check against. This is the
actual bug, and it is independent of which capability option below is chosen.

### 2. The advertised budgets are wrong by roughly 200×

`get_folder_info` is the tool the agent was explicitly told to call first to
learn its limits. Queried against the live server just now:

```json
{"mcp_inline_limit_chars": 100000, "mcp_base64_limit_bytes": 1048576, ...}
```

`MCP_BASE64_LIMIT_BYTES` defaults to 1 MiB (`app/config.py:49`, `:83`). The
real ceiling is whatever the model can emit in one tool call — observed here at
roughly **5 KB of decoded bytes**. The server is advertising 1 MiB of write
capacity that cannot be used, to the one tool whose job is to report what can.

That number came from `docs/plan-mcp-connector.md`'s "Binary files are worse
than large ones" section, and it was wrong when written: it was picked as a
*context-cost* budget without asking what a model can actually generate. The
incident is the correction.

**The ceiling is not a constant.** It varies by model, by surface, and by how
much of the context window is already spent. Anything that hardcodes a number
will be wrong somewhere. That is an argument for detecting the failure (defect
1) rather than predicting it — but the advertised number should still stop
being a fiction.

### 3. The same mistake on the read side, not yet triggered

`read_file` with `encoding="base64"` (`app/mcp_endpoint.py:348-360`) has **no
truncation guard at all**. The text path has one — over budget returns
`{preview, bytes, truncated, note}` — but the base64 path either returns the
entire thing or raises. So `read_file("x.pdf", encoding="base64")` on a 900 KB
file would hand the model 1.2 MB of base64 in a single tool result.

Nobody has hit this yet because the agent was reading files it had just
written. It is the same wrong assumption pointed the other way, and it should
be fixed in the same pass.

---

## The floor: make the failure loud (do this regardless)

**This is not one of the options. It is the fix for the defect that caused the
incident, and every option below assumes it.**

Give `write_file` a way to verify what it received:

```python
def write_file(path, content, ctx, encoding="utf-8", parents=True,
               overwrite=True,
               expect_bytes: int | None = None,
               expect_sha256: str | None = None) -> dict:
```

- After decoding, compare `len(data)` to `expect_bytes` and
  `hashlib.sha256(data).hexdigest()` to `expect_sha256`. On mismatch, **raise
  before `atomic_write` is entered** — nothing is created, so there is no
  corrupt file to notice and delete.
- The error message must name truncation as the likely cause and say what to do
  about it, since that is overwhelmingly what a short write means:
  *"Received 4,875 bytes but expect_bytes said 28,160 — the content was almost
  certainly truncated in transit. Nothing was written."*
- **`read_file` returns `sha256` and `bytes` on every result**, base64 and text
  alike. This is what makes the check free for the copy case: the model is not
  computing a hash, it is forwarding two values it was handed. Truncate the
  forwarded content and the numbers no longer match.
- **Require at least one of them for base64 writes above a small threshold**
  (say 1 KB decoded). A model generating novel binary content cannot produce a
  hash for it — so the arguments stay optional in general — but a base64 write
  is nearly always a copy of bytes that came from somewhere, and somewhere can
  supply a length.

`expect_bytes` alone catches truncation with certainty, since a truncated
payload is always shorter. `expect_sha256` additionally catches transposition
and single-character damage. Both are a few lines; there is no reason to pick
between them.

Also in this pass: **set the budgets to something true.** Lower the default
`MCP_BASE64_LIMIT_BYTES` to ~8 KB, add the read-side truncation guard from
defect 3, and have `get_folder_info` report the write ceiling as advisory with
a note that the real limit is the caller's own output budget, which the server
cannot see.

---

## Options for the capability gap

The floor turns a corrupt file into a clean error. It does **not** make the
28 KB PDF writable. That needs one of these.

### Option A — `copy_file`: move bytes server-side, never through the model

```
copy_file(src, dst, overwrite=False, parents=True)
```

Both paths are inside the shared folder, so the bytes never enter a tool result
at all. Implementation is `Vault.open_read` (`app/safepath.py:380`) into
`Vault.atomic_write` (`app/safepath.py:410`) — both exist, both already
enforce containment, and the copy inherits the atomic temp-then-rename
guarantee for free. Perhaps 30 lines including the REST endpoint.

- **Fixes the reported case exactly.** The PDF was already in the folder; the
  task was to duplicate it to an archive path. That is a copy, and it should
  never have involved the model carrying bytes.
- **No size limit whatsoever.** A 200 MB file copies as fast as a 200 byte one.
- **Limited scope:** only helps when the source is already in the folder. Does
  nothing for new content the model generates.

### Option B — chunked write sessions: the general fix

```
begin_write(path, overwrite=True, parents=True)  -> {"upload_id": "..."}
write_chunk(upload_id, seq, content, encoding)   -> {"received_bytes": N}
commit_write(upload_id, expect_bytes, expect_sha256) -> {"ok": true, ...}
abort_write(upload_id)
```

**This does not break the atomicity guarantee, and it is worth being precise
about why.** `docs/plan-mcp-connector.md` rejected `append_file` on the grounds
that atomic replace is a documented promise implemented as write-temp-then-
`rename` (`app/safepath.py:442-453`). A chunked session is *that same
mechanism*, held open across several calls: writes land in the same
`.upload-*.tmp` file, and `commit_write` performs the same single `rename`.
A reader still never sees a partial file. Appending to a live file would break
the promise; this does not.

- **Removes the ceiling for generated content**, at roughly 5 KB per call. A
  28 KB PDF is 6 calls. A 1 MB file is ~200 calls, which is not realistic — so
  this raises the practical limit into the low hundreds of KB, not to parity
  with REST.
- **Costs cross-request state**, which is the real price. Specifically:
  - It fights `stateless_http=True` (chosen in the connector plan for good
    reasons). The session state would live in the app, keyed by `upload_id`,
    not in the MCP transport — workable, but it is new mutable state in a
    container that deliberately has almost none.
  - Abandoned sessions leak temp files and open file descriptors. There is an
    existing sweep for exactly this shape of garbage
    (`Vault.sweep_temp_files`, `app/safepath.py:179`) but it runs only at
    startup; this needs an age-based reaper and a cap on concurrent sessions.
  - `seq` must be checked for gaps and reordering, or chunked writes reintroduce
    silent corruption in a new costume — which would be an embarrassing way to
    fix this particular bug.
- Easily the largest of the four. Worth it only if writing model-generated
  files of tens of KB is a real, recurring need rather than this one incident.

### Option C — `write_from_url`: the server fetches it

```
write_from_url(path, url, expect_sha256=None)
```

Zero bytes through the model and **no size ceiling at all** — the server
streams the download straight into `atomic_write`.

**This is the one with a real security cost, and it should not be chosen
casually.** It gives outbound network capability to a container whose entire
premise is that it is the one thing an agent can reach. That means SSRF: an
agent that can name a URL can make this container fetch it, including
`169.254.169.254`, `localhost`, and anything else on the host's network.

Worth knowing: `mcp` already pulled `httpx`, `httpcore` and `certifi` into the
image (flagged in `docs/plan-mcp-connector.md`, decision gate 3), so the
capability is a config change away whether or not anyone decided on it. That is
an argument for deciding deliberately, not for treating it as already paid for.

If chosen, it needs all of: an explicit host allowlist (empty by default, so
the tool is inert until configured), redirects disabled entirely, a
`Content-Length` preflight against `MAX_UPLOAD_BYTES`, a hard byte cap during
streaming regardless of what the header claimed, refusal of every non-public IP
range after DNS resolution (re-checked at connect time, or the check is a TOCTOU
hole), and a timeout. That list is the actual cost of this option.

### Option D — no new capability; just make the limits honest

Ship the floor, drop `MCP_BASE64_LIMIT_BYTES` to something real (~8 KB), and
have `write_file`'s description and `get_folder_info` state plainly that files
above it go over REST (`PUT /v1/content/{path}`) rather than through a tool.

- **Cheapest, and not a cop-out.** The connector plan's premise was always that
  `/mcp` is a convenience layer and REST remains the bulk path. This option
  says: that was right, the caps were just set to the wrong numbers.
- **The real question it turns on:** can the agents that actually use this make
  HTTP calls? A Claude Code or Cowork session with a shell can `curl` the REST
  API and never needs any of A/B/C. A browser-based claude.ai session cannot,
  and for it this option means "files over ~8 KB are not writable, ever."
- Leaves the incident's actual task — archiving a 28 KB PDF — unsolved for any
  surface without a shell.

### Option E — upload tickets (listed to dismiss)

`create_upload_ticket(path)` returning a short-lived single-use URL, which the
agent then `PUT`s to over plain HTTP. It removes the byte ceiling and adds no
outbound capability.

**But it only works where the agent can make arbitrary HTTP calls — which is
precisely the case where it did not need the MCP connector in the first place.**
It adds a ticket-issuing endpoint, ticket state, and expiry, to serve a caller
that could already have used the bearer token directly. Mentioned so the
absence is visibly deliberate rather than an oversight.

---

## Comparison

| | Fixes the reported case | Helps generated content | Size ceiling | New attack surface | Rough size |
|---|---|---|---|---|---|
| **Floor** (integrity) | turns corruption into a clean error | same | unchanged | none | ~40 lines |
| **A** `copy_file` | **yes, completely** | no | none | none | ~30 lines |
| **B** chunked writes | yes | **yes** | ~low hundreds of KB | new cross-request state | largest |
| **C** `write_from_url` | yes | only if it is fetchable | **none** | **outbound net / SSRF** | medium + hardening |
| **D** honest limits | no | no | ~8 KB | none | ~10 lines |
| **E** tickets | only with a shell | only with a shell | none | ticket state | medium |

## Recommendation

**Floor + A**, and defer B until there is a second incident that A would not
have fixed.

The reasoning is that the reported failure was a *copy*, not a generation: the
bytes already existed on the server and the model was made into a courier for
them. `copy_file` deletes that role entirely, for files of any size, at almost
no cost and with no new attack surface. Pairing it with the integrity floor
means the one remaining way to produce a corrupt file — a genuinely
model-generated payload that truncates — now fails loudly with nothing written.

B is the honest general answer and is worth building if archiving
model-generated documents of tens of KB turns out to be routine. It is also the
one that can quietly reintroduce this same class of bug through chunk
ordering, so it deserves to be its own piece of work with its own tests rather
than a rider on this fix.

C should wait for a requirement that names it. The SSRF surface is a genuine
change to what this container is, and "we might want to fetch things" is not
enough to justify it in a service whose README argues its own containment.

---

## Separate issue: the deleted file is not recoverable from here

The agent noted, correctly, that it deleted the corrupt PDF and had no tool to
check the good version back out of git history. That gap is real and worth
recording, though note that **with the floor in place this situation does not
arise** — the corrupt file is never created, so the original is never
overwritten and never needs restoring.

For the general case, the constraint is structural: the API container
bind-mounts only the shared subfolder and refuses `.git` as a path component
(`DENIED_NAMES`, confirmed live). It genuinely cannot see the repository. Only
the `autocommit/` sidecar mounts the repo root.

Three ways out, in preference order:

1. **Do nothing; document it.** Recovery is `git checkout` on the host, by a
   human. One line in `AGENT.md` telling an agent to report the path and stop,
   rather than improvise. Free.
2. **A read-only history surface on the sidecar** — `list_versions(path)` /
   `restore_version(path, ref)` — as its own small service. Keeps `.git` out of
   the API container entirely, which is the property worth protecting.
3. **Give the API container the repo root.** No. That is the exact mount this
   project exists to avoid, and `README.md`'s security model would have to be
   rewritten around it.

Also worth a line in `delete_file`'s description: when a write looks wrong, the
better move is usually to leave the bad file and say so, rather than delete it —
a deletion is one more thing to undo, and the auto-commit sidecar will push the
deletion too. The agent's instinct here was reasonable, but "stop and report"
beats "clean up" when the recovery path is unclear.

---

## What changes where

Assuming Floor + A:

| File | Change |
|---|---|
| `app/mcp_endpoint.py:401` | `write_file` gains `expect_bytes` / `expect_sha256`, verified before `atomic_write` is entered |
| `app/mcp_endpoint.py:326` | `read_file` returns `sha256` + `bytes` on every path; base64 path gets the truncation guard it lacks |
| `app/mcp_endpoint.py` | new `copy_file` tool |
| `app/mcp_endpoint.py:260` | `get_folder_info` reports the new advisory write ceiling and says the real bound is the caller's output budget |
| `app/config.py:49`, `:83` | `MCP_BASE64_LIMIT_BYTES` default 1 MiB → ~8 KB |
| `app/main.py` | `POST /v1/copy` alongside `/v1/move` (`app/main.py:462`), reusing `MoveRequest`'s shape, so REST and MCP stay one implementation |
| `app/safepath.py` | a `Vault.copy()` built from `open_read` + `atomic_write`; no new path handling |
| `tests/mcp_escape_suite.py` | see below |
| `tests/escape_suite.py` | `copy_file` added to the escape matrix — a copy is a write, and every planted symlink and `..` vector must be tried against **both** `src` and `dst` |
| `README.md`, `AGENT.md`, `.env.example` | the new tool, the corrected budgets, the "report don't improvise" note |
| `docs/plan-mcp-connector.md` | correct the 1 MiB figure and the reasoning that produced it — that document is why the number was wrong |

## Tests

1. **A base64 write truncated to a multiple of 4 is refused.** The regression
   test for the incident. Take a real payload, cut it at a length where
   `len % 4 == 0`, pass the *original* `expect_bytes`, assert the call fails
   **and that no file was created** — the second half is the point.
2. **The same cut without `expect_bytes`** is refused too, once the payload is
   over the small-write threshold, rather than silently writing short.
3. **Round-trip integrity:** `read_file` → `write_file` forwarding the returned
   `sha256`/`bytes` succeeds, and the destination is byte-identical to the
   source.
4. **`copy_file` containment**, in both container modes (`isolated` and
   `app-only`), against `src` and `dst` independently: `..`, absolute paths,
   NUL, planted symlinks, hard links. A copy that reads through a symlink is an
   escape even if the write is contained.
5. **`copy_file` is write-scoped** — refused with a read-only token.
6. **`copy_file` is atomic**: no `.upload-*.tmp` left behind on failure, and an
   interrupted copy leaves the destination absent rather than partial.
7. **Read-side guard:** a binary file over the base64 cap is refused with the
   REST pointer, not returned whole.
8. **The advertised budget is the enforced one** — `get_folder_info`'s numbers
   and the values `write_file`/`read_file` actually enforce come from the same
   config, asserted together so they cannot drift again. That drift is defect 2.

Same style as the existing suites: a standalone script with a `Results`
accumulator and a non-zero exit.

## Open questions

1. **Which option?** That is the decision this document exists for.
2. **Is ~8 KB the right advisory cap?** It is a guess from one data point
   (~4,875 bytes landed, at an unknown fraction of a context window). Worth
   measuring across the surfaces actually in use before pinning a default —
   and worth remembering that the floor's integrity check is what makes being
   wrong about this survivable.
3. **Should `write_file` refuse base64 entirely above the cap**, or accept it
   with a warning in the result? Refusing is more honest and is what the rest
   of this connector does; the counter-argument is that a caller who knows its
   own budget is being denied something that would work. Leaning refuse.
