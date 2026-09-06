# claude-files

A small REST API, in a hardened container, that gives an agent
upload / download / replace / delete access to **exactly one folder** and
nothing else. Built to hand Claude cloud processes a write target inside a
git repository while the rest of the repository stays unreachable.

56 checks pass in each of two container configurations, including every
escape attempt in `tests/escape_suite.py`. See
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
docker compose up -d --build
```

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

### 2. Build the image on the unRAID box

There's no published image, so build it once:

```bash
cd /mnt/user/appdata && git clone <this-repo> claude-files-src && docker build -t claude-files:1.0.0 /mnt/user/appdata/claude-files-src
```

### 3. Generate a token

```bash
openssl rand -hex 32
```

### 4. Add the container

Copy `unraid/claude-files.xml` to `/boot/config/plugins/dockerMan/templates-user/`
and it appears under **Docker → Add Container → Template**. Or add it by hand
with these settings:

| Field | Value |
|---|---|
| Repository | `claude-files:1.0.0` |
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
| `PUT` | `/v1/content/{path}` | Create **or replace**. Raw body. |
| `POST` | `/v1/content/{path}` | Create only; `409` if it exists. |
| `DELETE` | `/v1/content/{path}` | Delete a file or dir. `?recursive=true` for non-empty dirs. |
| `POST` | `/v1/mkdir/{path}` | Create a directory. `?parents=true` by default. |
| `POST` | `/v1/move` | `{"src":…, "dst":…, "overwrite":false}` |

Writes are atomic: content goes to a temp file in the destination directory
and is `rename()`d into place, so a reader never sees a half-written file.

```bash
TOKEN=your-token-here
API=https://files.example.com

curl -H "Authorization: Bearer $TOKEN" "$API/v1/list?recursive=true"
curl -H "Authorization: Bearer $TOKEN" -X PUT --data-binary @report.md "$API/v1/content/reports/report.md"
curl -H "Authorization: Bearer $TOKEN" "$API/v1/content/reports/report.md" -o report.md
curl -H "Authorization: Bearer $TOKEN" -X DELETE "$API/v1/content/reports/report.md"
```

Errors are JSON: `{"code": "traversal", "message": "..."}`. Every rejection is
logged at `WARNING`, so escape attempts show up in `docker logs`.

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

### Verifying it yourself

```bash
bash tests/run_tests.sh
```

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
| `RATE_LIMIT_PER_MINUTE` | `600` | `0` disables. |
| `LOG_LEVEL` | `INFO` | |
