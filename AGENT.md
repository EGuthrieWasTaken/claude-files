# File API access

You have write access to one shared folder on a remote machine. **If you have
an MCP connector for this service (tools named `list_files`, `read_file`,
`write_file`, ...), use those instead of the REST calls below** — they're
typed, discoverable, and don't need this brief pasted into context. This
document is for the REST fallback: when no connector is available, and for
files too large or too binary for a tool result (the connector's tools say so
explicitly when that happens, and name the REST endpoint below to use
instead).

Use it whenever you need to hand back a file rather than paste content
into the conversation: reports, generated data, build artifacts, logs.

**Base URL:** `https://claude-files.guthrieec.dev`
**Auth:** every request except `/health` needs `Authorization: Bearer $CLAUDE_FILES_TOKEN`
(or `X-API-Key: $CLAUDE_FILES_TOKEN` — the header the MCP connector dialog uses;
`Authorization` is the documented primary and wins if both are sent).

The token is a credential. Never write it into a file you upload, never put
it in a URL or query string, and never echo it into output that gets saved.

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
| `GET` | `/v1/info` | Current limits and policy |

Full spec: `GET /openapi.json`.

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
| `404` | File doesn't exist |
| `409` | `no_parent` (retry with `?parents=true`), `POST` onto an existing file, or deleting a non-empty dir without `?recursive=true` |
| `413` | Body exceeds the upload limit |
| `429` | Rate limited — back off and retry |
| `507` | Out of disk space or quota |

Retry `429` and `507` after a pause. Do **not** retry `403` with a modified
path hoping to get through; fix the path or stop.

## Constraints

- Max upload **256 MiB** per file. Check `/v1/info` for the live value.
- Rate limit **600 requests/minute** shared across all clients.
- `.git`, `.gitmodules`, and `.ssh` are refused as path components anywhere.
- Filenames matching `.upload-*.tmp` are reserved; don't create them.

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
