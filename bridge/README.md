# claude-files-bridge

A local client that streams files to/from a claude-files server without the
bytes ever passing through a model's own output. Full design brief:
[`docs/feature-request-path-based-upload.md`](../docs/feature-request-path-based-upload.md).

**Run this on the agent's own machine — a cloud workspace, a laptop, a
phone with a shell — never on the claude-files server itself.** It holds
your `API_TOKEN`. Only configure it somewhere that token already belongs.

## Install

Nothing to install for `upload`/`download`/`check` — `claude_files_bridge.py`
is stdlib-only. Get it with one `curl`, authenticated with your read token:

```bash
curl -sf -H "Authorization: Bearer $CLAUDE_FILES_TOKEN" \
  -o claude_files_bridge.py "$CLAUDE_FILES_URL/v1/bridge.py"
```

`serve` mode (a local MCP server) additionally needs:

```bash
pip install -r requirements.txt   # mcp==1.12.4
```

## Configure

Environment variables only — never a tool argument or a CLI flag, so a
credential never lands in a transcript, a log, or an agent's own config file.

| Variable | Required | Meaning |
|---|---|---|
| `CLAUDE_FILES_URL` | yes | Base URL, e.g. `https://files.example.com`. Trailing slash tolerated. |
| `CLAUDE_FILES_TOKEN` | one of these two | `API_TOKEN` (rw) or `API_TOKEN_READONLY` (ro — download only). |
| `CLAUDE_FILES_TOKEN_FILE` | one of these two | Path to a file whose *stripped* contents are the token. Wins if both are set — this is what keeps the secret out of `.mcp.json`, which is frequently committed. |
| `CLAUDE_FILES_TIMEOUT` | no | Per-request socket timeout in seconds. Default `60`. |

TLS verification is always on. There is no flag to disable it — if
verification fails, the bridge says which CA bundle it used and stops. If
your network hands out a private CA, point `SSL_CERT_FILE` or
`REQUESTS_CA_BUNDLE` at it (checked in that order).

## Three ways to run it

| Mode | For | Invocation |
|---|---|---|
| **stdio MCP server** | Claude Code (desktop, cloud sessions, IDE), anything that can launch a local MCP server | `python3 claude_files_bridge.py serve` |
| **CLI** | any surface with a shell but no local-MCP support | `python3 claude_files_bridge.py upload ./brief.pdf briefs/2026/brief.pdf` |
| **import** | tests, and anyone scripting it | `from claude_files_bridge import upload, download, check` |

### Claude Code (desktop, cloud session, IDE)

`.mcp.json` in the project, or `claude mcp add`:

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

This exposes three tools: `upload_file(local_path, remote_path, overwrite=True,
parents=True)`, `download_file(remote_path, local_path, overwrite=False)`, and
`bridge_status()` — call the last one first if an upload fails, it diagnoses a
broken setup (URL, token, proxy, CA bundle, server version and limits) in one
call.

### A surface with a shell but no local MCP

The two-line recipe above, once per session, then:

```bash
python3 claude_files_bridge.py upload ./report.pdf reports/2026/report.pdf
python3 claude_files_bridge.py download reports/2026/report.pdf ./report.pdf
python3 claude_files_bridge.py check --json
```

Add `--json` to any subcommand for a single parseable JSON object instead of
prose.

### A surface with neither a shell nor a local process

The bridge cannot run there — it is code that must run somewhere. Use the
connector's `begin_write`/`write_chunk`/`commit_write` tools instead; slower
and ceiling-bound, but correct.

## Exit codes (CLI mode)

| Code | Meaning |
|---|---|
| `0` | Success |
| `1` | Usage or configuration error (missing URL/token, bad arguments, a hostile `remote_path`) |
| `2` | Local file problem (missing, unreadable, a directory, over the server's `max_upload_bytes`) |
| `3` | Server refusal — any `4xx`; the JSON `code`/`message` is printed |
| `4` | Transport failure after retries (connection, DNS, TLS, `5xx`) |
| `5` | Integrity mismatch — what landed is not what was sent |

## What this does not fix

- **A sandbox with no outbound network to the server.** If nothing can reach
  `CLAUDE_FILES_URL`, no client-side design helps — the bytes have nowhere to
  go. `check` (or the `bridge_status` tool) makes that diagnosis take one call
  instead of an afternoon.
- **A surface with no filesystem and no shell.** The bridge is code that must
  run somewhere. Where nothing can run, the connector's
  `begin_write`/`write_chunk`/`commit_write` remain the only path.
- **The token reaching a new machine.** The bridge holds a credential
  wherever it runs — not a new risk class (any user of the connector already
  holds the token), but a new *location*. Use `CLAUDE_FILES_TOKEN_FILE`, and
  `API_TOKEN_READONLY` if you only need downloads.
- **Recovering an overwritten file.** The bridge makes a *corrupt* overwrite
  essentially impossible (the server verifies `X-Expect-Sha256` before
  renaming into place); it does nothing about a *deliberate* one.
