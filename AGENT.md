# File API access

You have write access to one shared folder on a remote machine. **If you have
an MCP connector for this service (tools named `list_files`, `read_file`,
`write_file`, ...), use those instead of the REST calls below** — they're
typed, discoverable, and don't need this brief pasted into context. This
document is for the REST fallback: when no connector is available, and for
files too large or too binary for a tool result (the connector's tools say so
explicitly when that happens, and name the REST endpoint below to use
instead).

If the connector's `write_file` refuses a file as too large (or your own
output would clearly overrun a few KB in one call), use its
`begin_write`/`write_chunk`/`commit_write`/`abort_write` tools instead of
forcing it through one inline call — `commit_write` needs the byte count or a
sha256 hash to verify against. Copying a file that's already in the folder?
Use `copy_file` (or `POST /v1/copy` below) instead of reading it out and
writing it back in — the bytes never pass through your context either way.

Use it whenever you need to hand back a file rather than paste content
into the conversation: reports, generated data, build artifacts, logs.

**Base URL:** `https://claude-files.guthrieec.dev`
**Auth:** every request except `/health` needs `Authorization: Bearer $CLAUDE_FILES_TOKEN`
(or `X-API-Key: $CLAUDE_FILES_TOKEN` — the header the MCP connector dialog uses;
`Authorization` is the documented primary and wins if both are sent).

The token is a credential. Never write it into a file you upload, never put
it in a URL or query string, and never echo it into output that gets saved.

## Uploading an existing file

If the file already exists on disk (you didn't just generate its content),
**don't paste its bytes into a tool call.** For anything over a few KB that's
slow and error-prone: every byte costs output tokens, and re-typing dense
text or base64 drops or corrupts characters. Use this three-step flow
instead — the bytes go straight from your shell to the server and never pass
through you:

1. **Request a ticket** (needs the bearer token):

   ```
   POST /v1/uploads
   {"path": "reports/q3/data.parquet"}
   -> {"upload_id": "u_3f9c…",
       "upload_url": "https://.../v1/uploads/u_3f9c…/<secret>",
       "curl_command": "curl -sS --fail -T <LOCAL_FILE> 'https://.../v1/uploads/u_3f9c…/<secret>'",
       "expires_at": 1790000000.0, "max_bytes": 268435456}
   ```

2. **Run the `curl_command` from your shell**, with the local path filled in.
   No `Authorization` header, no bearer token — the secret in the URL is the
   one-time authorization for that upload, and it's good for one file, once.

   ```
   curl -sS --fail -T ./data.parquet 'https://.../v1/uploads/u_3f9c…/<secret>'
   -> {"ok": true, "upload_id": "u_3f9c…", "path": "reports/q3/data.parquet",
       "size": 52428800, "sha256": "..."}
   ```

3. **Verify.** Compare `sha256sum ./data.parquet` against the `sha256` in
   that response, or poll `GET /v1/uploads/{upload_id}` (bearer token
   required) if you want to check progress on a large upload first —
   `bytes_received` climbs while it streams, then `state` becomes `complete`.

If you have a shell and the bearer token but no local `curl_command` handy,
the equivalent is `curl -H "Authorization: Bearer $CLAUDE_FILES_TOKEN" --data-binary @file -X PUT "$API/v1/content/<path>?parents=true"` —
same idea, just authenticated with the long-lived token instead of a
one-time ticket.

A ticket is for one path, once: it expires if you never use it, and a second
upload against the same ticket is rejected.

## Paths

Every path is **relative to the shared folder root**, with no leading slash.
`reports/q3/summary.md` is valid; `/reports/...` and `../anything` are not.

The API is confined to that one folder and will reject anything that tries to
leave it. Don't attempt traversal, absolute paths, or symlinks — they return
`403` and are logged. There is nothing above the shared folder you are meant
to reach, so a `403` here means your path is malformed, not that you need a
different approach.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/v1/list` or `/v1/list/{dir}` | List a directory. `?recursive=true`, `?limit=N` |
| `GET` | `/v1/stat/{path}` | Type, size, mtime for one entry |
| `GET` | `/v1/content/{path}` | Download a file (raw bytes) |
| `PUT` | `/v1/content/{path}` | Create **or replace**. Body is the raw file content. `?parents=true` |
| `POST` | `/v1/content/{path}` | Create only — `409` if it already exists. `?parents=true` |
| `DELETE` | `/v1/content/{path}` | Delete. `?recursive=true` for a non-empty directory |
| `POST` | `/v1/mkdir/{path}` | Create a directory (parents created by default) |
| `POST` | `/v1/move` | Body: `{"src":"a.txt","dst":"b.txt","overwrite":false,"parents":false}` |
| `POST` | `/v1/copy` | Body: `{"src":"a.txt","dst":"b.txt","overwrite":false,"parents":false}` — server-side copy, no size ceiling |
| `POST` | `/v1/uploads` | Create a one-time upload ticket for an existing local file |
| `PUT`/`POST` | `/v1/uploads/{id}/{secret}` | Upload the bytes for a ticket. **No bearer token.** |
| `GET` | `/v1/uploads/{id}` | Ticket status and progress |
| `GET` | `/v1/info` | Current limits and policy |
| `GET` | `/v1/bridge.py` | Download the local upload/download bridge — see "Uploading a file you generated" |

Full spec: `GET /openapi.json`.

## Uploading a file you generated

If you generated a file yourself (a PDF, an image, a build artifact) and can
run shell commands or launch a local MCP server, **do not base64 it through
`write_file` or a connector's `write_chunk`** — your own per-call output
ceiling is the binding limit here, not anything this server enforces, and it
is well under 10 KB. Instead:

```bash
curl -sf -H "Authorization: Bearer $CLAUDE_FILES_TOKEN" \
  -o claude_files_bridge.py "$API/v1/bridge.py"
python3 claude_files_bridge.py upload ./report.pdf reports/report.pdf
```

That streams the bytes straight from your own disk and verifies a sha256
server-side — never through your context. Full reference: `bridge/README.md`.
This only applies if you have a shell or can launch a local process; if you
don't (a browser-only surface talking to the remote connector), the
connector's `begin_write`/`write_chunk`/`commit_write` tools remain the way.

## Writing into a folder that doesn't exist yet

Writes do not create parent directories by default. **Add `?parents=true` and
they will** — it works like `mkdir -p`, and it's the normal way to write to a
new path in one call:

```
PUT /v1/content/notes/2026/q3/report.md?parents=true   (body: hello world)
  -> {"ok":true,"path":"notes/2026/q3/report.md","size":11,"created":true}

GET /v1/content/notes/2026/q3/report.md
  -> hello world
```

Without the flag, a missing parent gives you a `409` that names the folder:

```
PUT /v1/content/notes/2026/q3/report.md
  -> 409 {"code":"no_parent",
          "message":"Parent directory 'notes/2026/q3' does not exist
                     (missing at 'notes'). Retry with parents enabled ..."}
```

That is not a path-safety rejection — just retry with `?parents=true`, or
create the folder explicitly with `POST /v1/mkdir/notes/2026/q3`. `mkdir` is
safe to call on a directory that already exists; it returns `created:false`
rather than an error.

`?parents=true` works on `PUT` and `POST`. For `/v1/move`, pass
`"parents": true` in the JSON body instead.

## Reading

```
GET /v1/list/notes/2026
  -> {"path":"notes/2026",
      "entries":[{"name":"a.txt","path":"notes/2026/a.txt","type":"file",
                  "size":11,"mtime":1788666621.78,"readable":true}],
      "truncated":false}
```

`type` is `file`, `dir`, `symlink`, or `other`. **Only entries with
`readable:true` can be downloaded** — symlinks and other non-regular files are
listed for completeness but refused on read. `truncated:true` means you hit
`limit`; narrow the path or raise it.

There is no partial read, append, or patch. To modify a file, `GET` it, change
it locally, and `PUT` the whole thing back. Writes are atomic, so a reader
never sees a half-written file.

## Errors

Every error is JSON: `{"code":"...","message":"..."}`.

| Status | Meaning |
|---|---|
| `401` | Missing or wrong token |
| `403` | Path rejected (`traversal`, `absolute_path`, `symlink`, `denied_name`) or token is read-only |
| `404` | File doesn't exist, or (on `/v1/uploads/{id}/{secret}`) a bad ticket id or secret |
| `409` | `no_parent` (retry with `?parents=true`), `POST` onto an existing file, deleting a non-empty dir without `?recursive=true`, or reusing an upload ticket that isn't `pending` |
| `410` | An upload ticket expired before it was used — request a new one |
| `413` | Body exceeds the upload limit |
| `422` | `integrity_mismatch` — you sent `X-Expect-Bytes`/`X-Expect-Sha256` and what arrived didn't match; or `sha_mismatch` — an upload ticket's bytes didn't match its `expect_sha256`. Nothing was written either way |
| `429` | Rate limited — back off and retry |
| `507` | Out of disk space or quota |

Retry `429` and `507` after a pause. Do **not** retry `403` with a modified
path hoping to get through; fix the path or stop.

## Constraints

- Max upload **256 MiB** per file. Check `/v1/info` for the live value.
- Rate limit **600 requests/minute** shared across all clients.
- `.git`, `.gitmodules`, and `.ssh` are refused as path components anywhere.
- Filenames matching `.upload-*.tmp` are reserved; don't create them.
- An upload ticket is good for one path, once, and expires 15 minutes after
  creation if never used (`/v1/info` reports the live TTL and how many
  tickets can be outstanding at once).

## This folder is a git repository

Anything you write is **automatically committed and pushed within a few
minutes**. Two consequences:

1. **Writes are effectively permanent and public.** They enter git history on
   a remote. Never write credentials, tokens, keys, or personal data.
2. **Prefer few, complete writes over many partial ones.** Don't use the
   folder as scratch space — assemble the file, then upload it once. Deleting
   a file later removes it from the working tree but not from history.

Mass deletions are refused by a safety guard, so don't try to clear the folder
wholesale; delete only what you specifically intend to.
