# claude-files

[![CI](https://github.com/EGuthrieWasTaken/claude-files/actions/workflows/ci.yml/badge.svg)](https://github.com/EGuthrieWasTaken/claude-files/actions/workflows/ci.yml)

A small REST API, in a hardened container, that gives an agent
upload / download / replace / delete access to **exactly one folder** and
nothing else. Built to hand Claude cloud processes a write target inside a
git repository while the rest of the repository stays unreachable.

Every escape attempt in `tests/escape_suite.py` and `tests/mcp_escape_suite.py`
passes in each of two container configurations, on every pull request (see the
CI badge above, and `.github/workflows/ci.yml`). See
[Security model](#security-model).

---

## Should you just use something off the shelf?

Worth being straight about this: **the bind mount is what actually contains
an agent**, and any of these get that part right if you mount only the
subfolder. The differences are in API ergonomics and blast radius.

| Option | Verdict |
|---|---|
| **[`hacdias/webdav`](https://github.com/hacdias/webdav)** | The closest ready-made fit. Tiny Go WebDAV server, scoped `directory:`, per-user rules, basic auth. `GET`/`PUT`/`DELETE`/`MKCOL` work fine from `curl`. **Use this if you want zero custom code.** Its `rules` are prefix matches, not a containment proof, and it follows symlinks by default. |
| **[`filebrowser/filebrowser`](https://filebrowser.org)** | Has a REST API and per-user scopes, but auth is a JWT login flow rather than a static bearer token, and you're also exposing a full web UI you don't need. More surface than the job requires. |
| **[`drakkan/sftpgo`](https://github.com/drakkan/sftpgo)** | Genuinely good, with a real REST API, virtual folders, quotas, and per-user permissions. Heavier than this needs to be, and its own admin API is another thing to lock down. |
| **MinIO / S3** | Object storage, not files in a git working tree. Wrong shape. |
| **`claude-files`** (this repo) | Static bearer token, an OpenAPI spec an agent can read, symlink and hard-link refusal, and a test suite that proves the containment. Small enough to audit in one sitting. |

If you want the shortest path and are comfortable with WebDAV verbs, run
`hacdias/webdav` with the subfolder bind-mounted and stop there. The rest of
this README is for running the custom one.

---

## Quick start

```bash
cp .env.example .env && openssl rand -hex 32
```

Put that token in `.env`, point the volume in `docker-compose.yml` at your
subfolder, then:

```bash
docker compose up -d
```

That pulls the published image. To run your own build instead, uncomment
`build: .` in `docker-compose.yml` and use `docker compose up -d --build`.

Verify:

```bash
curl -s localhost:8080/health
```

---

## unRAID setup

### 1. Pick the folder to expose

Decide which subfolder of the repo the agent may touch. Everything outside it
will be invisible.

```
/mnt/user/appdata/repos/myrepo/          <- repo root, stays private
├── .git/                                <- stays private
├── src/                                 <- stays private
└── shared/                              <- THE ONLY THING YOU MOUNT
```

If the folder doesn't exist yet:

```bash
mkdir -p /mnt/user/appdata/repos/myrepo/shared && chown 99:100 /mnt/user/appdata/repos/myrepo/shared
```

### 2. Nothing to build

The image is published multi-arch on Docker Hub, so unRAID pulls `linux/amd64`
on its own:

```bash
docker pull eguthriewastaken2/claude-files:latest
```

### 3. Generate a token

```bash
openssl rand -hex 32
```

### 4. Add the container

Copy the templates to `/boot/config/plugins/dockerMan/templates-user/` and
they appear under **Docker → Add Container → Template**:

```bash
cp unraid/claude-files.xml unraid/claude-files-autocommit.xml /boot/config/plugins/dockerMan/templates-user/
```

`claude-files-autocommit.xml` is the optional sidecar covered in
[Auto-committing what agents write](#auto-committing-what-agents-write); skip
it if you only want the API. To add the API by hand instead:

| Field | Value |
|---|---|
| Repository | `eguthriewastaken2/claude-files:latest` |
| Network Type | `bridge` |
| Port | `8080` → `8080` |
| Path | **Container** `/data` → **Host** `/mnt/user/appdata/repos/myrepo/shared` (Read/Write) |
| Variable | `API_TOKEN` = the token from step 3 |
| Extra Parameters | `--read-only --tmpfs /tmp:rw,noexec,nosuid,nodev,size=64m --cap-drop ALL --security-opt no-new-privileges:true --pids-limit 200 --memory 512m --user 99:100` |

> **The single most important field is the Host Path.** Point it at the
> subfolder. If you point it at the repo root, you have handed the agent the
> whole repository and none of the rest of this matters.

`--user 99:100` makes written files owned by `nobody:users`, which is what the
rest of unRAID expects. The container refuses to start if `/data` isn't a
mount point, so a typo in the path fails loudly instead of silently accepting
uploads into a directory that disappears with the container.

### 5. Expose it to Claude's cloud processes

You already run `cloudflared` on this box, so the tidiest route is a hostname
on the existing tunnel — no inbound port forward:

```yaml
# in your cloudflared config
ingress:
  - hostname: files.example.com
    service: http://claude-files:8080
  - service: http_status:404
```

Put a Cloudflare Access policy in front of it as a second gate if you can.
Either way the bearer token is required, so **use TLS** — the token is sent in
a header on every request.

---

## API

Interactive docs at `/docs`, machine-readable spec at `/openapi.json`. Every
path is relative to the shared folder root.

| Method | Path | Does |
|---|---|---|
| `GET` | `/health` | Liveness. No auth. |
| `GET` | `/v1/info` | Server limits and policy. |
| `GET` | `/v1/list[/{path}]` | List a directory. `?recursive=true`, `?limit=` |
| `GET` | `/v1/stat/{path}` | Type, size, mtime for one path. |
| `GET` | `/v1/content/{path}` | Download a file. |
| `HEAD` | `/v1/content/{path}` | Size and mtime as headers. |
| `PUT` | `/v1/content/{path}` | Create **or replace**. Raw body. `?parents=true` to create missing parents. Optional `X-Expect-Bytes` / `X-Expect-Sha256` headers, verified before the rename — see below. |
| `POST` | `/v1/content/{path}` | Create only; `409` if it exists. `?parents=true`. Same optional `X-Expect-Bytes` / `X-Expect-Sha256`. |
| `DELETE` | `/v1/content/{path}` | Delete a file or dir. `?recursive=true` for non-empty dirs. |
| `POST` | `/v1/mkdir/{path}` | Create a directory. `?parents=true` by default. |
| `POST` | `/v1/move` | `{"src":…, "dst":…, "overwrite":false, "parents":false}` |
| `POST` | `/v1/copy` | `{"src":…, "dst":…, "overwrite":false, "parents":false}` — server-side copy, no size ceiling. |
| `GET` | `/v1/bridge.py` | Download the local upload/download bridge — see [Uploading binary files: the local bridge](#uploading-binary-files-the-local-bridge). |
| `POST`/`GET`/`DELETE` | `/mcp` | Optional Streamable HTTP MCP connector — see [MCP connector](#mcp-connector). |
| `POST` | `/v1/uploads` | Create a one-time upload ticket. Needs the rw token. |
| `PUT`/`POST` | `/v1/uploads/{id}/{secret}` | Upload the bytes for a ticket. No bearer token — the secret in the URL is the authorization. |
| `GET` | `/v1/uploads/{id}` | Ticket status: `pending`/`receiving`/`complete`/`failed`/`expired`, bytes received, size, sha256. |
| `POST` | `/v1/downloads` | Create a one-time download ticket. Any token, including read-only. |
| `GET` | `/v1/downloads/{id}/{secret}` | Download the bytes for a ticket. No bearer token — the secret in the URL is the authorization. |
| `GET` | `/v1/downloads/{id}` | Ticket status: `pending`/`streaming`/`complete`/`failed`/`expired`, bytes sent, size, sha256. |

Writes are atomic: content goes to a temp file in the destination directory
and is `rename()`d into place, so a reader never sees a half-written file.

Optionally send `X-Expect-Bytes: <n>` and/or `X-Expect-Sha256: <64 hex
chars>` on a `PUT`/`POST` — verified against what was actually streamed,
*before* the rename, so a request that arrives complete but wrong (a
truncated proxy response, a file mutated mid-upload) is a clean `422
integrity_mismatch` refusal instead of a silently short or wrong file.
Every successful write's `OkResponse` carries `sha256` regardless of
whether you sent either header, so a caller can confirm what actually
landed without computing anything itself.

```bash
TOKEN=your-token-here
API=https://files.example.com

curl -H "Authorization: Bearer $TOKEN" "$API/v1/list?recursive=true"
curl -H "Authorization: Bearer $TOKEN" -X PUT --data-binary @report.md "$API/v1/content/reports/report.md"
curl -H "Authorization: Bearer $TOKEN" "$API/v1/content/reports/report.md" -o report.md
curl -H "Authorization: Bearer $TOKEN" -X DELETE "$API/v1/content/reports/report.md"
```

Writing into a directory that does not exist returns `409 no_parent` naming
the missing folder, rather than a bare `404` that reads as "file not found".
Pass `?parents=true` to create the chain in the same call, like `mkdir -p`.

Errors are JSON: `{"code": "traversal", "message": "..."}`. Every rejection is
logged at `WARNING`, so escape attempts show up in `docker logs`.

### Uploading an existing local file

`PUT /v1/content/{path}` works, but it means holding the bearer token and
sending the whole body in one request. For an agent that can't easily do
that -- or that shouldn't be trusted with the long-lived token -- ask for a
one-time upload ticket instead:

```bash
curl -H "Authorization: Bearer $TOKEN" -X POST "$API/v1/uploads" \
     -H "Content-Type: application/json" \
     -d '{"path": "reports/q3/data.parquet"}'
# -> {"upload_id": "u_3f9c…",
#     "upload_url": "https://.../v1/uploads/u_3f9c…/<secret>",
#     "curl_command": "curl -sS --fail-with-body -T <LOCAL_FILE> '.../v1/uploads/u_3f9c…/<secret>'",
#     "expires_at": ..., "max_bytes": ...}

curl -sS --fail-with-body -T ./data.parquet "https://.../v1/uploads/u_3f9c…/<secret>"
# -> {"ok": true, "upload_id": "u_3f9c…", "path": "reports/q3/data.parquet",
#     "size": ..., "sha256": "..."}
```

The `PUT` to `/v1/uploads/{id}/{secret}` needs **no bearer token** -- the
secret in the URL is single-use authorization for exactly that one path. The
ticket expires after `UPLOAD_TICKET_TTL_SECONDS` if never used, and can be
polled with `GET /v1/uploads/{id}` while it's in flight. See
[`docs/upload-urls.md`](docs/upload-urls.md) for the full design and
`AGENT.md` for the agent-facing walkthrough.

### Downloading a file to a local shell

The mirror image of the above, for files `read_file` can't return: PDFs and
other binaries, and text over the inline budget. Ask for a one-time download
ticket (any token works, read-only included):

```bash
curl -H "Authorization: Bearer $TOKEN" -X POST "$API/v1/downloads" \
     -H "Content-Type: application/json" \
     -d '{"path": "stat519/syllabus.pdf"}'
# -> {"download_id": "d_3f9c…",
#     "download_url": "https://.../v1/downloads/d_3f9c…/<secret>",
#     "curl_command": "curl -sS --fail-with-body -o <OUTPUT_FILE> '.../v1/downloads/d_3f9c…/<secret>'",
#     "path": "stat519/syllabus.pdf", "size": ..., "sha256": "...", "expires_at": ...}

curl -sS --fail-with-body -o syllabus.pdf "https://.../v1/downloads/d_3f9c…/<secret>"
sha256sum syllabus.pdf        # must equal the sha256 returned with the ticket
```

The `GET` needs **no bearer token**; the secret is single-use authorization
for that one file and expires after `DOWNLOAD_TICKET_TTL_SECONDS` if unused.
The ticket records the file's size and mtime when it is issued, and the
download is refused with `409 file_changed` if the file was replaced in
between, so the returned `sha256` always describes the bytes you receive.
Poll `GET /v1/downloads/{id}` for progress. See
[`docs/download-urls.md`](docs/download-urls.md).

`AGENT.md` in this repo is a paste-ready brief for handing to an agent.

---

## MCP connector

`/mcp` is a second front end onto the same shared folder, for agent surfaces
that speak MCP tools rather than composing raw HTTP calls — Cowork, the
claude.ai web app, and mobile all fall into this bucket. It does not replace
the REST API above; anything too large or too binary for a tool result still
goes over REST, and the MCP tools say so explicitly when that happens. Full
design brief: `docs/plan-mcp-connector.md`.

**Two hard limitations, both structural, not bugs to fix later:**

1. **Tool results are JSON in a model's context window.** There's no bulk
   transfer path, so `/mcp` enforces its own budget well below
   `MAX_UPLOAD_BYTES` (`MCP_INLINE_LIMIT_CHARS`, default 100 000 characters).
   A file over that budget comes back as a preview, the real byte count, and a
   note pointing at the REST download endpoint — never a silent cut.
2. **Binary is worse than large.** `read_file` decodes UTF-8 by default and
   refuses a file with a NUL byte or that fails to decode, naming the REST
   endpoint instead of returning mojibake. `encoding="base64"` opts in for a
   small binary file, capped far below the text budget
   (`MCP_BASE64_LIMIT_BYTES`, default **8 KiB** — a single-shot-call ceiling,
   not a context-cost budget; see below) — base64 inflates size by a third
   and buys nothing since the model can't read it anyway.

### Large and binary writes: the integrity floor, and chunked sessions

An agent's own output is bounded by whatever it can emit in one tool call —
observed well under 8 KiB of decoded bytes on at least one real surface. A
`write_file` call that gets cut at that boundary can still be **valid
base64** (a truncated base64 string decodes cleanly whenever its length is a
multiple of 4), so a naive truncation guard never fires; the file is written
short, silently. `docs/plan-mcp-large-writes.md` has the full incident and
root-cause writeup. Two things fix it:

1. **`write_file` and `read_file` carry integrity metadata.** `write_file`
   accepts `expect_bytes` and/or `expect_sha256`, checked against the decoded
   content **before** anything is written — a mismatch leaves nothing on
   disk, rather than a short file to notice and clean up after. Required (at
   least one) for a base64 write over 1 KB decoded. `read_file` returns
   `bytes` and `sha256` on every successful result, so a copy can forward
   them into `write_file` without computing anything itself. A base64
   `write_file` over `MCP_BASE64_LIMIT_BYTES` decoded is refused outright,
   naming `begin_write` as the next step.
2. **`begin_write` / `write_chunk` / `commit_write` / `abort_write`** open a
   chunked upload session for anything larger. `write_chunk` calls must
   arrive in order (`seq` starting at 0, no gaps, no repeats — a retried
   chunk after a failed call is the one case a real caller hits, and resending
   the same `seq` is the correct fix). Base64 chunks are sliced only on
   4-character boundaries so each decodes standalone without buffering the
   whole file in memory. `commit_write` requires `expect_bytes` and/or
   `expect_sha256` — stricter than `write_file`, deliberately: anything worth
   chunking is worth verifying. A session left idle past
   `MCP_UPLOAD_SESSION_TTL_SECONDS` is reaped lazily (no background task); an
   `upload_id` is bound to the credential that opened it.

   **This does not weaken the atomicity guarantee.** A chunked session holds
   open the exact same write-temp-then-`rename` mechanism every other write in
   this API uses — chunks land in one `.upload-*.tmp` file, and `commit_write`
   performs the one `rename` that makes the destination exist. A reader never
   sees a partial file, whether the write was one call or fifty. There is
   **no REST equivalent** for the four chunked tools, deliberately: `PUT
   /v1/content/{path}` already streams an arbitrarily large raw body with no
   output-token ceiling, so REST has no version of this problem to solve.

`get_folder_info` reports all of the above (`mcp_base64_limit_bytes` and a
note that it's advisory, plus `mcp_upload_chunk_advisory_bytes`) so a caller
never has to guess.

### Uploading existing files: `request_upload`

The preferred way for a connector caller to upload a file that already
exists on its machine. `request_upload(path)` mints the same one-time ticket
as `POST /v1/uploads` (see [Uploading an existing local
file](#uploading-an-existing-local-file)) and returns its `upload_url` and a
ready-made `curl_command`; the agent runs that from its shell, and the bytes
go straight over HTTP instead of through its own output. `upload_status`
reports progress and the server-computed `sha256`, which the agent compares
against its local `sha256sum`. Unlike the bridge below, the agent's shell
never needs the long-lived token -- only `curl` and outbound access to this
server. `write_file` now also returns the `sha256` it wrote.

The `upload_url`'s host comes from `PUBLIC_BASE_URL`, else
`MCP_PUBLIC_HOSTNAME` (as `https://`), else the request's forwarded host.
Set one of the first two in production. If Cloudflare Access fronts the
hostname, `/v1/uploads/*/*` needs a bypass rule, since `curl` in an agent's
sandbox has no Access session.

### Reading binary or large files: `request_download`

The way for a connector caller to read a file `read_file` can't return --
a PDF, an image, anything with NUL bytes, or text over the inline budget.
`request_download(path)` mints the same one-time ticket as `POST
/v1/downloads` (see [Downloading a file to a local
shell](#downloading-a-file-to-a-local-shell)) and returns `download_url`,
`size`, `sha256` and a `curl_command` with an `<OUTPUT_FILE>` placeholder.
`download_status` reports progress. For short reads there are two lighter
options: `read_file(path, pages="1-3")` returns a PDF's extracted text, and
`read_file(path, offset=…, length=…)` reads a big text file in pieces
(`next_offset` tells you where to resume). It works with a read-only token, and the
agent's shell needs no token at all. `read_file`'s refusals name this tool.
URL host and Cloudflare Access notes are the same as for `request_upload`
(bypass `/v1/downloads/*/*` too).

### Uploading binary files: the local bridge

The constraint above ("Tool results are JSON in a model's context window")
means every MCP write is generated as base64 by the calling model, and the
binding limit on that is the model's own per-call **output** ceiling —
observed well under 10 KB — not any setting on this server.
`MCP_BASE64_LIMIT_BYTES` and `begin_write`/`write_chunk`/`commit_write` are
about *this server's* budgets; they cannot raise a ceiling that lives on the
other side of the connection. Full design brief:
[`docs/feature-request-path-based-upload.md`](docs/feature-request-path-based-upload.md).

`bridge/claude_files_bridge.py` is a small, stdlib-only client that runs in
the *agent's own environment* — a cloud workspace, a laptop, anything with a
filesystem — and streams bytes straight to `PUT`/`GET /v1/content/{path}`,
never through the model's output. Get it with one authenticated `curl` and
run it:

```bash
curl -sf -H "Authorization: Bearer $CLAUDE_FILES_TOKEN" \
  -o claude_files_bridge.py "$API/v1/bridge.py"
python3 claude_files_bridge.py upload ./brief.pdf briefs/2026/brief.pdf
python3 claude_files_bridge.py download briefs/2026/brief.pdf ./brief.pdf
```

As a local MCP server (Claude Code desktop, cloud sessions, IDE) it exposes
three tools instead — `upload_file`, `download_file`, and `bridge_status`
(call this one first if an upload fails; it diagnoses the URL, token, proxy,
CA bundle, and server limits in one call):

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

Full install/config/exit-code reference: `bridge/README.md`. **The bridge
runs on the agent's machine and holds the token there** — only configure it
somewhere that token already belongs. It does not replace
`begin_write`/`write_chunk`/`commit_write`, which remain the only path on a
surface with no shell and no local process at all (e.g. the claude.ai web
app talking only to the remote connector) — the bridge *supplements* the
chunked path, it doesn't retire it.

`GET /v1/bridge.py` (read auth) hands out the script itself, and `GET
/v1/info` reports `bridge_available` so a deployment can be confirmed to
serve it without probing further.

### Copying within the folder

`copy_file` (MCP) and `POST /v1/copy` (REST, body `{"src":…, "dst":…,
"overwrite":false, "parents":false}`) copy a file server-side — the bytes
never pass through the tool call or a model's context, so there's no size
ceiling. Useful for re-archiving or duplicating a file that's already in the
folder, which is cheaper than reading it out and writing it back in.

### Git tools

Optional (off by default): the agent can inspect and synchronise the git
repository that contains its folder — `git_status`, `git_commit`, `git_pull`,
`git_push`, `git_merge`, `git_fetch`, `git_log`, `git_diff` — as MCP tools and
matching `/v1/git/*` REST routes. **Nothing is committed or pushed
automatically; the agent runs every git operation itself.** Full design:
`docs/plan-mcp-git-ops.md`.

Two independent switches. A disabled class is not registered at all, so its
tools are absent from the tool list rather than present and refusing:

| Variable | Tools | Token |
|---|---|---|
| `ENABLE_GIT_READ` | `git_status`, `git_log`, `git_diff` | works with the read-only token |
| `ENABLE_GIT_WRITE` | `git_commit`, `git_pull`, `git_push`, `git_merge`, `git_fetch` | needs the read-write token |

(`git_fetch` counts as a write — it moves refs and uses the deploy key — so
`git_status(fetch=true)` needs the write switch and a read-write token.)

**The idea: your folder is a *scope* inside the repository, and no write may
leave it.** Say the repository holds `sub-folder-1/` (the folder this service
serves) and `sub-folder-2/` (someone else's work):

- `git_status` reports changes **across the whole repository**, each marked
  `in_scope` or not, so the agent can see that `sub-folder-2` changed. This is
  a deliberate read across the boundary, and it discloses file *names* only:
  `git_diff` shows only `sub-folder-1`, always.
- Every **write** is refused if it would touch anything outside the folder. It
  works out what git *would* change and refuses **before** touching anything:
  a commit is built from a temporary index containing only the folder (a file
  a person staged in `sub-folder-2` can't ride along); a pull or merge is
  computed with `git merge-tree` and refused if it would change outside paths
  or conflict, so a conflicted merge is never left half-applied; a push is
  refused if any commit it would publish touches outside paths (checked per
  commit, so a host-side edit-then-revert is still refused).
- The price of that rule: if someone pushes a change to `sub-folder-2`
  upstream, the agent **can't pull it**, and until someone pulls on the host
  its pushes are rejected as non-fast-forward. The refusal names the paths and
  says so. `GIT_PULL_OUTSIDE_SCOPE=true` (below) lifts this for remote
  content only.

#### Mount layout: let the kernel enforce the boundary too

The application enforces the scope, but the recommended deployment makes the
kernel enforce it as well — three nested bind mounts, so that **no process in
the container** can write outside `.git` and the shared folder:

```yaml
volumes:
  - /mnt/user/appdata/repos/myrepo:/repo:ro                     # whole repo, READ-ONLY
  - /mnt/user/appdata/repos/myrepo/.git:/repo/.git              # git needs to write here
  - /mnt/user/appdata/repos/myrepo/shared:/repo/shared          # the scope
  - /mnt/user/appdata/keys/deploy_ed25519:/keys/id_ed25519:ro
  - /mnt/user/appdata/keys/known_hosts:/keys/known_hosts:ro
environment:
  DATA_ROOT: "/repo/shared"
  GIT_REPO_ROOT: "/repo"
  ENABLE_GIT_READ: "true"
  ENABLE_GIT_WRITE: "true"
  GIT_REMOTE: "origin"
  GIT_BRANCH: "main"
  SSH_KEY_PATH: "/keys/id_ed25519"
  SSH_KNOWN_HOSTS_PATH: "/keys/known_hosts"
```

The scope is **derived** — `DATA_ROOT` relative to `GIT_REPO_ROOT` — so there
is no separate scope setting that could disagree with the folder the file
tools serve. If the shared folder *is* the repository root, leave
`GIT_REPO_ROOT` unset; the scope is the whole repository.

This **changes the first layer of the [security model](#security-model)**:
the rest of the repository is no longer *absent* from the container, only
read-only, and `.git` is present and writable (git can't work without it). A
bug in the path layer could therefore let the agent *read* the rest of the
repository, and a bug that let a file tool write into `.git` would be code
execution through the next git call. The path layer is the barrier for both,
and `.git` is refused by it at any depth (and git mode refuses to start
otherwise). If the root is mounted read-write instead, git mode still works
and every software check still applies, but the server logs a warning and
reports `write_boundary: "software"` in `get_folder_info`.

Use a **deploy key scoped to this one repository** — the credential now lives
in the same process the agent talks to. `SSH_KNOWN_HOSTS_PATH` is required
with a key; unlike the autocommit sidecar, this container never trusts a host
key on first use.

#### How git is kept from becoming a way out

Git runs programs named in config (hooks, `core.fsmonitor`, filters, `ssh`
commands, credential helpers, pagers). So git is **never** run with the
container's environment, never with repository discovery, and with hooks and
every program-naming setting forced off on the command line, which beats any
config file. **Hooks never run**, and there is no setting to enable them: a
hook that ran repo code would execute files the agent wrote. At startup the
server refuses to run if `.git` isn't in `DENIED_NAMES`, if git's config
`[include]`s a file inside the working tree (which an agent could write), if
`.git` is a gitlink file, or if the remote isn't `ssh://`, `https://`, or
scp-style (never `file://`, `ext::`, or a local path). The agent never
supplies a remote, URL, refspec, or flag; the only thing it chooses is
`git_merge`'s branch name, validated against a strict character allow-list
and resolved to a SHA before git sees it. Push is never forced, and nothing
in the module can reset, clean, check out, delete a branch, or force. Output
returned to the agent is scrubbed of credentials.

#### `GIT_PULL_OUTSIDE_SCOPE`

Off by default. When `true`, `git_pull` (and `git_merge` of an
`origin/<branch>`) may update files outside the folder, but **only with
content that already exists on the remote**: the result at every outside path
must be byte-identical to what's already in `HEAD` or to what the remote has.
The agent still can't *author* anything outside its folder. It refuses when
the result would be content neither side wrote (a three-way merge of a file
both a person and the remote changed), when merging a *local* branch that
touches outside paths, and when it would overwrite a person's uncommitted
edit. A rebase that would rewrite a host-made commit touching outside paths
is refused in both modes. It needs the repo root mounted read-write.

#### Working alongside everything else

- **Uploads:** `git_pull` and `git_merge` refuse while any chunked
  `begin_write` session or `request_upload` ticket is open
  (`uploads_in_flight`, naming them) — a pull could rename a folder an upload
  is writing into. `git_commit` always excludes `.upload-*.tmp`.
- **Writes during a git operation:** file writes take a shared gate and the
  commit/merge step takes it exclusively, so a write arriving mid-merge waits
  up to 10 s and then fails `423 git_busy`; it never interleaves.
- **Host-side git:** a person running git on the host is respected. The
  index lock is never deleted (`locked` is reported), `HEAD` moving
  underneath is caught by a compare-and-swap on commit, and a host
  `checkout`, detached `HEAD`, or half-finished merge makes the write tools
  refuse (`wrong_branch`, `detached_head`, `operation_in_progress`).
- **The autocommit sidecar:** with `ENABLE_GIT_WRITE` on, stop the sidecar for
  that repository — the two would race on the index lock and the agent's
  picture of what's pushed would go stale. With only `ENABLE_GIT_READ`, they
  coexist fine. (`get_folder_info`'s instruction text stops claiming writes are
  "committed and pushed automatically" when the write tools are on.)
- Run a **single worker** (the image does): the locks are in-process.

Errors carry a stable `code` — `outside_scope`, `outside_scope_commits`,
`non_fast_forward`, `diverged`, `conflict`, `dirty_worktree`,
`uploads_in_flight`, `nothing_to_commit`, `too_many_changes`, `invalid_ref`,
`stale_remote`, `locked`, `git_busy`, `auth_failed`, `network`, `timeout` —
with the paths involved and the next step in the message. The image needs
`git` and `openssh-client`, installed by default (`--build-arg WITH_GIT=0`
leaves them out, and then enabling a git switch is a startup error that says
so). Tests: [Verifying it yourself](#verifying-it-yourself).

### Extension modules

A separate project can add its own tools to `/mcp` without forking this repo.
It builds a derived image `FROM` a pinned claude-files tag, copies a Python
package in, and sets `EXTRA_TOOL_MODULES` to that package. Its `register(api)`
adds tools that sit beside the core ones, behind the same token check,
reading and writing through the same path-contained `Vault` and worktree
gate. A tool registered with `read_only=False` is refused for a read-only
token before the extension's code runs. `get_folder_info` reports what
loaded. A broken extension stops the server from starting rather than
quietly leaving its tools out.

The contract, its gotchas, and a minimal example are in
[`docs/extensions.md`](docs/extensions.md).

### Adding it as a connector

1. Set `ENABLE_MCP=true` (the default) and, if this service sits behind a
   reverse proxy or tunnel, `MCP_PUBLIC_HOSTNAME` to its public hostname —
   otherwise the MCP SDK's DNS-rebinding guard is disabled outright rather
   than 421ing every real request (`/mcp` is still gated by `API_TOKEN`
   either way).
2. If Cloudflare Access sits in front of this hostname, add a **Bypass
   policy scoped to `/mcp` only** — the app's own token check is the gate;
   don't disable Access on the whole hostname for this.
3. In claude.ai / Cowork, add a custom connector: URL is `https://<host>/mcp`,
   auth is "No sign-in" plus a static header — `x-api-key` is one of its
   preset header names — holding `API_TOKEN` (or `API_TOKEN_READONLY` for a
   read-only connector).

Every REST route also accepts `X-API-Key: <token>` now, as an alternative to
`Authorization: Bearer <token>` — that's what the connector dialog sends.
`Authorization` remains the documented primary and wins if both are present
on one request.

`GET /v1/info` reports `mcp_enabled` so you can confirm what a deployment is
actually serving without probing `/mcp` directly.

---

## Security model

Three layers, all on by default. Layer 2 alone blocks every test in the
suite — that is what the `app-only` test configuration below demonstrates.
Layer 1 alone blocks all of them *except* the hard-link vector, since a hard
link inside the mounted folder is a real file in that folder as far as the
mount namespace is concerned. That's precisely why both exist.

**1. The mount namespace.** The container bind-mounts only the subfolder. The
rest of the repository is not merely forbidden, it is *absent* — there is no
path from inside the container that names it. The suite proves this by
listing `/data/..` inside a running container and showing the repo isn't
there.

**2. Path resolution that can't be tricked.** No request path is ever
concatenated into a string and handed to the OS. Components are validated
(`..`, absolute paths, NUL bytes, backslashes, over-long names all rejected)
and then walked **one component at a time with `openat` against a directory
file descriptor pinned at startup**, each step carrying `O_NOFOLLOW`. A
symlink anywhere in the path fails rather than being followed, and because
each step is a file descriptor rather than a name, there's no
time-of-check/time-of-use window in which a component can be swapped for a
symlink mid-request. `unlink` never follows links, and `rename` replaces the
destination entry itself, so writing to a symlink replaces the link instead of
writing through it.

**3. Container hardening.** Read-only root filesystem, all capabilities
dropped, `no-new-privileges`, non-root user, pid and memory limits, tmpfs
`/tmp` mounted `noexec,nosuid,nodev`.

Also on by default: `.git`, `.gitmodules`, and `.ssh` are refused as path
components at any depth, and regular files with more than one hard link are
refused, because a hard link created on the host can alias a file outside the
mounted subtree. Set `ALLOW_HARDLINKED_FILES=true` only if you have a reason.

**With the [git tools](#git-tools) on, layer 1 changes shape:** git needs the
repository, so the rest of it is mounted *read-only* rather than being absent,
and `.git` is present and writable. Layer 2 (which refuses `.git` at any depth)
is then the only barrier for both. The recommended three-mount layout keeps the
kernel enforcing "nothing but `.git` and the shared folder is writable"; see
[Mount layout](#mount-layout-let-the-kernel-enforce-the-boundary-too).

The optional `/mcp` connector (see [MCP connector](#mcp-connector)) is the
same three layers behind the same token check — it is a second front end onto
the same `Vault`, not a second implementation of path handling. Every MCP
tool routes its filesystem calls through the identical `VAULT.components()`
validation the REST routes use, and a rejected path is still logged, just
tagged `denied MCP …` instead of `denied POST …` so the two surfaces stay
distinguishable in `docker logs`.

A chunked `begin_write` session validates the path once, the same way, and
holds the resulting directory file descriptor open for the session's whole
lifetime — `write_chunk` and `commit_write` never re-resolve the path. A
descriptor can't be swapped for a symlink after the fact, so this extends the
same TOCTOU-proof guarantee across a multi-call session instead of narrowing
it to a single request.

**Download tickets are the one deliberate exception to "every read needs the
bearer token,"** and upload tickets (below) the one for writes. `GET
/v1/downloads/{id}/{secret}` is read-only and scoped to one file; it applies the
same containment as every other read (symlinks, hard links, denied names
are refused when the ticket is issued *and* again when it is used), and
its secret is redacted from the access log exactly like an upload secret.

**Upload tickets are the one deliberate exception to "every write needs the
bearer token."** `PUT /v1/uploads/{id}/{secret}` accepts no `Authorization`
header at all -- the 256-bit secret in the URL is scoped to a single path, is
single-use, expires, and is compared with `hmac.compare_digest`. Creating a
ticket still requires the rw token and re-validates the path the same way
every other write does, and a symlink planted at the target after the ticket
was issued is still caught at upload time. The secret is redacted from
`uvicorn`'s access log before it's written anywhere.

### Verifying it yourself

```bash
bash tests/run_tests.sh
```

CI runs exactly this on every pull request (`.github/workflows/ci.yml`), plus
`tests/autocommit_tests.sh` against the sidecar image — there is no separate
CI-only test list to keep in sync.

The [git tools](#git-tools) add two suites that need only `git` and the app's
requirements (no Docker), and CI runs them as their own job:

```bash
python3 tests/git_suite.py            # app.git_ops against throwaway repos, real git
python3 tests/git_surface_tests.py    # the same tools through /mcp and REST, all four on/off combinations
```

`git_suite.py` covers the scope boundary (a commit with a person's staged file
in `sub-folder-2`, pulls and pushes that would cross it, a host commit and its
revert), the opt-in rule, planted hooks and config that name programs (each
test first proves the hook *would* fire under plain git), hostile merge refs,
every startup refusal, credential scrubbing, and a stalled remote. It reaches
its remote over git's *file* transport via test-only seams in `app.git_ops`
that production does not expose (it allows only ssh and https), so the ssh
transport itself is not exercised in-process. `tests/run_tests.sh` also runs
the image's own `git` in the three-mount layout and checks that the kernel
refuses a write outside the folder.

This builds the image and runs the suite against two configurations:

- **isolated** — the production layout, only the subfolder mounted.
- **app-only** — deliberately weakened: the *whole repo* is mounted at
  `/repo` with `DATA_ROOT=/repo/shared`, so the parent directory genuinely
  **is** reachable in the namespace. Everything still has to hold on the
  strength of layer 2 alone.

Both must pass. The fixture plants hostile symlinks (`-> ../secrets.env`,
`-> ..`, `-> /`) and a hard link to a file outside the folder, then the suite
attacks with raw `..` in the request line (sent via `http.client`, which
doesn't normalise it the way `curl` does), percent-encoded and double-encoded
traversal, overlong UTF-8, absolute paths, NUL-byte truncation, backslashes,
and reads/writes/moves/deletes through every planted link.

An escape test passes only if the canary string stayed out of the response
**and** the file outside the folder stayed byte-identical. A final integrity
check re-hashes everything outside the shared folder and compares.

### What this does not protect against

- **Anyone who can write to the host folder directly.** The API is one way in;
  it doesn't police the others.
- **A leaked token.** It's full read/write within the folder. Rotate by
  changing `API_TOKEN` and restarting. Use `API_TOKEN_READONLY` for consumers
  that only need to read.
- **Content itself.** An agent can write a malicious file into the shared
  folder. If something downstream executes what lands there, that's your
  boundary to think about, not this one.
- **A kernel or Docker escape.** Out of scope for any container.

---

## Auto-committing what agents write

`autocommit/` is an optional sidecar that polls the shared folder and commits
and pushes whatever appears. Point it at the same folder `claude-files`
serves and agent output lands in git on its own.

```bash
docker compose up -d autocommit
```

On unRAID, add `unraid/claude-files-autocommit.xml` as a second container.
Two fields matter:

| Field | Value |
|---|---|
| Repository Root | the repo root — `/mnt/user/appdata/repos/myrepo`, the folder containing `.git` |
| Commit Subfolder | `shared` — the same folder `claude-files` exposes |

Point the deploy key and `known_hosts` at host paths and leave the rest at
their defaults. If you declare an SSH key path but don't mount a key, the
container refuses to start and says so in the log rather than running without
push access.

It isn't tied to `claude-files`, either. Set Commit Subfolder to `.` and it
commits the whole repository instead — a general-purpose autocommit for any
git working tree on the server (see [whole-repository mode](#whole-repository-mode)).

**It polls rather than watching with inotify, deliberately.** unRAID's
`/mnt/user` is a FUSE layer, and inotify through it silently drops events —
and a missed event is a file that never gets committed, with nothing to tell
you. A poll cannot miss anything, and it batches a burst of forty uploads
into one commit instead of forty.

### What keeps it in scope

The sidecar mounts the **repo root**, because `.git` is what it needs to make
a commit. That's a wider mount than the API gets, so its containment is a
different mechanism: `SUBDIR` scopes every `git add`, so the sidecar can only
ever commit the same folder the API can write to. It refuses to start if
`SUBDIR` is empty, absolute, or contains `..` — a misconfiguration there
would silently widen it to the whole repository, so it fails loudly instead.
Widening it on purpose takes an explicit `SUBDIR=.` (below).

### Whole-repository mode

`SUBDIR=.` commits everything in the working tree that `.gitignore` doesn't
exclude, anywhere in the repo. It's for using the sidecar as a plain
autocommit for any repo — a config folder, notes, an appdata directory —
rather than as a `claude-files` companion. The container logs a warning at
startup so the wider scope is never a surprise.

In this mode the scope is whatever `.gitignore` says, so write it first:
anything a container keeps rewriting (logs, databases, caches) belongs in
it. Those files also don't hold the quiet period open — only files git
would actually commit count toward it. The folder must already be a git
repository with a branch checked out and a remote configured; the sidecar
doesn't create either. On a folder with a lot of existing uncommitted
files, the first poll will trip `MAX_CHANGED_FILES`, so make that first
commit by hand.

Also handled:

- **Bursts** — a `QUIET_PERIOD` defers the commit while files are still landing.
- **In-flight uploads** — `.upload-*.tmp` files are excluded by pathspec, so a
  poll never catches a half-written upload. Nothing is written to your
  `.gitignore`.
- **A diverged remote** — it pushes first and only rebases if that's rejected,
  so the working tree is untouched in the common case. On divergence it
  rebases with `--autostash`, which is required rather than optional: any
  unrelated uncommitted edit in the repo makes `rebase` refuse to start, and
  without it the sidecar would commit forever and never push again.
- **A runaway agent** — `MAX_CHANGED_FILES` (default 1000) refuses the commit
  and logs loudly rather than pushing a mass deletion.

### Configuration

| Variable | Default | Meaning |
|---|---|---|
| `REPO_PATH` | `/repo` | Repo working tree. Must contain `.git`. |
| `SUBDIR` | *required* | Folder to commit, relative to the repo root. `.` commits the whole repository. |
| `INTERVAL` | `120` | Seconds between polls. |
| `QUIET_PERIOD` | `30` | Skip if a file to be committed changed this recently. Ignored files don't count. `0` disables. |
| `GIT_BRANCH` | current branch | Must match what's checked out. |
| `GIT_REMOTE` | `origin` | Which remote to push to. Use a second SSH remote here to leave `origin` on HTTPS for host-side use. |
| `MAX_CHANGED_FILES` | `1000` | Runaway guard. `0` disables. |
| `SSH_KEY_PATH` | — | Deploy key, mounted read-only. Staged to tmpfs at 0600. |
| `SSH_KNOWN_HOSTS_PATH` | — | Pins the host key. Without it, trust-on-first-use with a warning. |
| `GIT_TOKEN` | — | PAT instead of a key. Held in tmpfs, never written to disk. |
| `PUSH` | `true` | `false` commits locally only. |
| `DRY_RUN` | `false` | Log what would be committed, change nothing. |
| `RUN_ONCE` | `false` | One pass and exit — use this to run it from cron instead. |

`RUN_ONCE=true` is there if you'd rather drive it from a cron job than leave a
daemon running. Same logic, no loop.

### Tests

```bash
bash tests/autocommit_tests.sh
```

21 checks against a real bare-repo remote: scoping, deletions, the quiet
period, temp-file exclusion, a diverged remote, a diverged remote *with*
unrelated dirty files (verifying the stashed edits come back and don't get
committed), the runaway guard, every unsafe `SUBDIR` plus detached HEAD
being refused at startup, whole-repository mode (`.gitignore` and temp-file
exclusion at every depth), and ignored files not holding the quiet period
open.

---

## Publishing a new version

> **1.2.0's release notes should say:** `EXTRA_TOOL_MODULES`
> ([extension modules](#extension-modules), [`docs/extensions.md`](docs/extensions.md))
> is new in this version: a derived image can add its own tools to `/mcp`, and
> `GET /v1/info` and `get_folder_info` gain an `extensions` block. Derived
> images should build `FROM eguthriewastaken2/claude-files:1.2.0` (or later),
> not `:latest`. Nothing changes for a deployment that leaves
> `EXTRA_TOOL_MODULES` unset. After redeploying, **verify from a client, not
> the host**: `GET /health` reports `"version":"1.2.0"`, and `GET /v1/info`
> has `"extensions": {"api_version": 1, ...}`. `unraid/deploy.sh verify`
> checks the version for you.

`unraid/deploy.sh` scripts the redeploy-and-verify half of this section —
`build` (the buildx command below), `pull` + recreate (via `docker compose`
if that's how the container is managed, otherwise it tells you to hit
unRAID's own "Force Update" instead of guessing at a template it can't see),
and `verify` (the client-side checks above). Run it with no argument for
`deploy` then `verify`, or `unraid/deploy.sh --help` for the full list.

> **Pulling this image starts serving the [MCP connector](#mcp-connector)**
> at `/mcp` by default (unRAID templates track `:latest`, so this happens
> without anyone asking for it). It's defensible — `/mcp` is gated by the
> same `API_TOKEN` as every other route — but say it plainly rather than let
> it be discovered. Set `ENABLE_MCP=false` to opt out, or build with
> `--build-arg WITH_MCP=0` for an image that doesn't have the `mcp` package
> at all.

The image is multi-arch (`linux/amd64` for unRAID, `linux/arm64` for local
work on Apple silicon). Build both in one shot and push a single manifest:

```bash
docker buildx build --platform linux/amd64,linux/arm64 --tag eguthriewastaken2/claude-files:1.2.0 --tag eguthriewastaken2/claude-files:latest --push .
```

Confirm both architectures landed under one tag:

```bash
docker buildx imagetools inspect eguthriewastaken2/claude-files:1.2.0
```

The sidecar is a separate image, built the same way:

```bash
docker buildx build --platform linux/amd64,linux/arm64 --tag eguthriewastaken2/claude-files-autocommit:1.2.0 --tag eguthriewastaken2/claude-files-autocommit:latest --push ./autocommit
```

Building on separate machines and pushing to the same tag does **not** produce
a multi-arch image -- the second push replaces the first. If you build
natively on each host, push arch-suffixed tags and merge them:

```bash
docker buildx imagetools create --tag eguthriewastaken2/claude-files:1.2.0 eguthriewastaken2/claude-files:1.2.0-arm64 eguthriewastaken2/claude-files:1.2.0-amd64
```

On Arch Linux, `docker buildx` is a separate package: `sudo pacman -S docker-buildx`.

The unRAID templates pull `:latest`, so always push both tags. `docker-compose.yml`
stays pinned to `:1.2.0` on purpose — compose deployments should be
reproducible, and `:latest` there would silently change what runs on the next
`up`.

---

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `API_TOKEN` | *required* | Bearer token, ≥16 chars. |
| `API_TOKEN_READONLY` | — | Optional read-only token. |
| `DATA_ROOT` | `/data` | Served directory. |
| `MAX_UPLOAD_BYTES` | `268435456` | 256 MiB. |
| `MAX_LIST_ENTRIES` | `10000` | Cap on entries per listing. |
| `DENIED_NAMES` | `.git,.gitmodules,.ssh` | Refused path components, any depth. |
| `ALLOW_DELETE` | `true` | `false` makes the API replace-only. |
| `ALLOW_HARDLINKED_FILES` | `false` | Leave false. |
| `REQUIRE_MOUNT` | `true` | Refuse to start if `DATA_ROOT` isn't a mount point. |
| `ENABLE_DOCS` | `true` | Serve `/docs` and `/openapi.json`. |
| `RATE_LIMIT_PER_MINUTE` | `600` | `0` disables. Shared between REST and `/mcp`. |
| `UPLOAD_TICKET_TTL_SECONDS` | `900` | How long an unused upload ticket stays valid. |
| `UPLOAD_TICKETS_MAX` | `100` | Cap on live upload tickets, so they can't use unbounded memory. |
| `MCP_PDF_MAX_BYTES` | `33554432` | Largest PDF `read_file(pages=…)` will parse (32 MiB). Larger ones: use `request_download`. |
| `DOWNLOAD_TICKET_TTL_SECONDS` | `900` | How long an unused download ticket stays valid. |
| `DOWNLOAD_TICKETS_MAX` | `100` | Cap on live download tickets. |
| `PUBLIC_BASE_URL` | *(unset)* | Base URL used to build `upload_url` and `download_url`. If unset, `https://$MCP_PUBLIC_HOSTNAME`, else derived from `X-Forwarded-Proto`/`X-Forwarded-Host` (or `Host`). |
| `LOG_LEVEL` | `INFO` | |
| `ENABLE_MCP` | `true` | Mount the [MCP connector](#mcp-connector) at `/mcp`. `false` disables it without rebuilding. |
| `MCP_PUBLIC_HOSTNAME` | — | This service's public hostname, for the MCP SDK's DNS-rebinding guard. Unset disables that guard rather than 421ing real requests. |
| `MCP_INLINE_LIMIT_CHARS` | `100000` | `read_file`'s inline text budget, in characters. |
| `MCP_BASE64_LIMIT_BYTES` | `8192` | `read_file`/`write_file`'s single-shot cap on source bytes when `encoding="base64"`. Advisory in practice — the real ceiling is what a caller can emit in one tool call. Over it, `write_file` refuses and names `begin_write`. |
| `MCP_UPLOAD_SESSION_TTL_SECONDS` | `900` | Idle timeout for a `begin_write` session, reaped lazily. |
| `MCP_MAX_CONCURRENT_UPLOADS` | `8` | Cap on open chunked-upload sessions at once. |
| `MCP_UPLOAD_CHUNK_ADVISORY_BYTES` | `3072` | Suggested chunk size, reported by `begin_write`/`get_folder_info`. Advisory, not enforced. |
| `EXTRA_TOOL_MODULES` | — | Comma-separated Python modules whose `register(api)` adds tools to `/mcp` at startup ([extension modules](#extension-modules)). Meant to be set by a derived image, not by hand. A module that fails to load stops the server from starting. |
| `ENABLE_GIT_READ` | `false` | Register `git_status`, `git_log`, `git_diff` ([git tools](#git-tools)). Works with the read-only token. |
| `ENABLE_GIT_WRITE` | `false` | Register `git_commit`, `git_pull`, `git_push`, `git_merge`, `git_fetch`. Needs the read-write token. Nothing is committed or pushed automatically. |
| `GIT_REPO_ROOT` | `DATA_ROOT` | Where the repository root is mounted. `DATA_ROOT` must be it or a folder under it; the difference is the agent's scope. |
| `GIT_REMOTE` | `origin` | The only remote ever fetched from or pushed to. Must be `ssh://`, `https://`, or scp-style. |
| `GIT_BRANCH` | checked-out branch | The only branch ever committed to or pushed. Re-checked on every write. |
| `GIT_AUTHOR_NAME` / `GIT_AUTHOR_EMAIL` | `claude-files agent` / `agent@localhost` | Author and committer of `git_commit`. The agent can't choose. |
| `GIT_COMMIT_TRAILER` | — | Optional trailer appended to every commit message. |
| `GIT_MAX_CHANGED_FILES` | `1000` | `git_commit` refuses to commit more files than this. `0` disables. |
| `GIT_NETWORK_TIMEOUT_SECONDS` | `60` | Bound on fetch/push. A timeout kills git's whole process group, `ssh` included. |
| `GIT_LOCAL_TIMEOUT_SECONDS` | `30` | Bound on every other git call. |
| `GIT_PULL_OUTSIDE_SCOPE` | `false` | Let pulls bring *remote* content into paths outside the folder. Needs the repo root mounted read-write. |
| `SSH_KEY_PATH` | — | Deploy key for an ssh remote, mounted read-only outside the repo. Staged to tmpfs at 0600. |
| `SSH_KNOWN_HOSTS_PATH` | — | **Required** with a key. No trust-on-first-use. |
| `GIT_TOKEN` | — | Token for an `https://` remote instead of a key. Kept off the command line and out of remote URLs. |

The [local bridge](#uploading-binary-files-the-local-bridge)'s own settings
(`CLAUDE_FILES_URL`, `CLAUDE_FILES_TOKEN`, etc.) are deliberately not in this
table — they're *client-side* environment, read by `bridge/claude_files_bridge.py`
wherever an agent runs it, never by this server. See `bridge/README.md`.
