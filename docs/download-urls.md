# Feature: one-time download URLs (out-of-band file reads)

**Status:** implemented (REST API, MCP tools, docs, tests) · **Type:** feature ·
**Affects:** `app/main.py`, `app/downloads.py`, `app/mcp_endpoint.py`, `app/config.py`,
`AGENT.md`, `README.md`

Read-side mirror of [`upload-urls.md`](upload-urls.md).

## Problem

`read_file` is the only MCP read path, and it returns file contents inline.
It cannot return PDFs (NUL bytes; the base64 route is capped at 8 KB) and cuts
off text over `MCP_INLINE_LIMIT_CHARS` (100,000 characters). In a course
folder that meant the homework-solution and syllabus PDFs were unreadable and
large HTML slide decks came back as a CSS-only preview.

## Design

```
agent ──MCP request_download(path)──▶ POST /v1/downloads
      ◀── {download_id, download_url, curl_command, size, sha256, expires_at}

agent shell:  curl -sS --fail-with-body -o out.pdf "<download_url>"   (bytes go direct)
agent shell:  sha256sum out.pdf   → compare with the returned sha256
```

- `POST /v1/downloads {"path"}` (any valid token, **read-only included**): validates
  the path with the full `Vault` checks (traversal, symlinks, hard links,
  denied names, regular file), hashes the file, records its size and
  `mtime_ns`, and mints a ticket.
- `GET /v1/downloads/{download_id}/{secret}` (**no bearer token**): the 256-bit
  secret in the path is the authorization, compared with `hmac.compare_digest`.
  Single use: `pending → streaming → complete | failed`. A second `GET` is
  `409`, an expired ticket `410`, a wrong id or secret `404`. The file is
  re-opened through `Vault.open_read` (so a symlink planted after issue is
  refused) and streamed with `Content-Length`, `X-File-Sha256` and an
  attachment `Content-Disposition`.
- `GET /v1/downloads/{download_id}` (bearer token): `state`, `bytes_sent`,
  `size`, `sha256`, `error`, timestamps.
- MCP: `request_download(path)` and `download_status(download_id)` wrap the same
  helpers (`issue_download_ticket`, `download_ticket_status`) as the REST routes.

### Consistency of `sha256`

The hash is computed when the ticket is issued. If the file's size or mtime
differs when the ticket is used, the download is refused (`409 file_changed`,
ticket marked `failed`) rather than served under a stale hash. A transfer that
ends early is marked `failed` with `interrupted`; request a new ticket to retry.

### Config

| Env var | Default | Purpose |
|---|---|---|
| `DOWNLOAD_TICKET_TTL_SECONDS` | `900` | How long an unused ticket stays valid |
| `DOWNLOAD_TICKETS_MAX` | `100` | Cap on live tickets |

Both are reported by `GET /v1/info`. The URL host comes from the same
`PUBLIC_BASE_URL` / `MCP_PUBLIC_HOSTNAME` logic as upload URLs; if Cloudflare
Access fronts the hostname, `/v1/downloads/*/*` needs the same bypass as
`/v1/uploads/*/*`.

## Security

- Read-only, so it adds nothing to the git history produced by auto-commit.
- Scope: one path, once. Path containment is enforced at issue **and** at use.
- Secret: kept out of the access log (`_RedactUploadSecrets` covers
  `/v1/downloads/{id}/{secret}`), and `PathError`s on that route are translated
  to `HTTPException` so the path-logging handler never sees the secret.
- Rate limiting still applies to the secret route.

## Companion changes to `read_file`

The same request listed two optional extras, implemented in `read_file`:

- **Ranged text reads.** `offset` (characters, default 0) and `length`
  (characters, clamped to the inline budget) return a slice plus
  `total_chars` and `next_offset` (`null` at the end). `bytes` and `sha256` still
  describe the whole file. Without them, behaviour is unchanged (full text
  under the budget, otherwise a preview whose note now mentions `offset`/`length`).
- **PDF text extraction.** A file starting with `%PDF-` is read through
  `pypdf` (pure Python, pinned in `requirements-mcp.txt`). `pages` takes
  `"3"`, `"2-5"`, `"1,3,7-9"` or `"5-"` (1-based). Pages are returned as
  `--- page N ---` sections until the per-call budget (`max_chars` / inline
  limit, and 50 pages) is used; `next_pages` can be passed straight back in.
  Scanned pages with no text layer are listed in `pages_without_text`.
  `offset`/`length` are refused on PDFs, and `pages` on non-PDFs.

Because PDFs are parsed in the server process, extraction is limited to files up
to `MCP_PDF_MAX_BYTES` (default 32 MiB) and 50 pages per call. Larger,
encrypted or unparseable PDFs are refused with a pointer to `request_download`.
