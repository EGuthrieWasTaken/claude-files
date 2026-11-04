# Plan: git status / commit / pull / push / merge tools, scoped to one folder

**Status: proposed.** Nothing in this document is implemented yet.

Follows [`docs/plan-mcp-connector.md`](plan-mcp-connector.md) and
[`docs/plan-mcp-large-writes.md`](plan-mcp-large-writes.md), both shipped.

---

## What this adds

The agent gets a small, fixed set of MCP tools (and matching REST routes) to
inspect and synchronise the git repository that contains its folder. The
agent runs every git operation itself; nothing commits or pushes in the
background.

| Tool | Class | What it does |
|---|---|---|
| `git_status` | read | Branch, upstream, ahead/behind, and changed files **across the whole repository**, each marked in or out of scope |
| `git_log` | read | The last *n* commits on the current branch |
| `git_diff` | read | Diff of the agent's folder against `HEAD`, size-capped like `read_file` |
| `git_commit` | write | Commit changes **inside the agent's folder only** |
| `git_pull` | write | Fetch the configured remote and integrate it, **only if nothing outside the folder would change** |
| `git_push` | write | Push the current branch, **only if every commit being published changes the folder and nothing else** |
| `git_merge` | write | Merge another branch, under the same rule as `git_pull` |
| `git_fetch` | write | Update remote-tracking refs only. The working tree is untouched. |

Two environment variables control them, independently:

| Variable | Default | Effect |
|---|---|---|
| `ENABLE_GIT_READ` | `false` | Registers `git_status`, `git_log`, `git_diff`. Usable with the read-only token. |
| `ENABLE_GIT_WRITE` | `false` | Registers `git_commit`, `git_pull`, `git_push`, `git_merge`, `git_fetch`. Needs the read-write token as well. |

A disabled class is **not registered**, so its tools don't appear in
`tools/list`. They aren't registered and then made to refuse. An agent can't
spend calls on a tool it can't see, and `get_folder_info` reports what's off
and why.

Push and pull matter most. They ship first, with `git_status` and `git_commit`
alongside, because a push needs something to push. See [Phasing](#phasing).

### Why read and write are split this way

The line follows **what an operation can change**, not which git verb it is:

- **Read** tools run no network I/O and change nothing in `.git` or the
  working tree (`GIT_OPTIONAL_LOCKS=0`, so not even the index stat cache).
  They are as safe as `list_files`, which is why the read-only token can use
  them.
- **Write** tools do one or more of these: change the working tree, move refs,
  write objects, or talk to the network. That puts `git_fetch` on the write
  side. It moves `refs/remotes/*` and makes an outbound connection with the
  deploy key. For the same reason `git_status(fetch=true)` needs both
  `ENABLE_GIT_WRITE` and an rw token.

With `ENABLE_GIT_WRITE=true` and `ENABLE_GIT_READ=false`, the server starts
but logs a warning. That combination is legal but odd. Every write tool's
result includes the post-operation branch summary (see
[Common result fields](#common-result-fields)), so it still works.

---

## The scope model

### The layout this is built for

```
my-repo/                    <- git root
├── .git/
├── sub-folder-1/           <- claude-files' folder: the agent's SCOPE
└── sub-folder-2/           <- outside scope: visible to git_status, never writable
```

The two rules:

1. **Reads may cross the scope boundary.** `git_status` reports changes
   anywhere in the repository, including uncommitted edits someone is making
   in `sub-folder-2`. That's accepted: the agent learns file names and change
   types outside its folder.
2. **Writes may not.** Nothing the agent can do, through the file tools or the
   git tools, may change a file outside `sub-folder-1`. That covers the host
   working tree and history published to the remote. Where git would need to
   do so, the tool refuses **before touching anything** and says why.

A repository whose root *is* the agent's folder is the degenerate case: the
scope is `.`, rule 2 is vacuous, and everything below still applies.

### How the scope is defined: no new variable

The scope is **derived**, not configured. A new variable, `GIT_REPO_ROOT`
(default: `DATA_ROOT`), names where the repository root is mounted. The scope
is `DATA_ROOT` relative to `GIT_REPO_ROOT`:

| `GIT_REPO_ROOT` | `DATA_ROOT` | Scope |
|---|---|---|
| `/repo` | `/repo/sub-folder-1` | `sub-folder-1/` |
| `/data` (default) | `/data` | `.` (whole repository) |

The file tools keep using `DATA_ROOT` exactly as today. `Vault`'s root fd is
pinned there, and `split_components()` (`app/safepath.py:96`) refuses `..`, so
they can't name `sub-folder-2` however the request is spelled. Neither the
agent nor the git tools can ever widen the scope.

### Mount layout: the kernel enforces the write boundary

The recommended deployment uses **three nested bind mounts**, so the write
boundary in rule 2 doesn't depend only on code:

```yaml
volumes:
  - /mnt/user/appdata/repos/my-repo:/repo:ro                            # whole repo, READ-ONLY
  - /mnt/user/appdata/repos/my-repo/.git:/repo/.git                     # rw: git needs it
  - /mnt/user/appdata/repos/my-repo/sub-folder-1:/repo/sub-folder-1     # rw: the scope
environment:
  GIT_REPO_ROOT: "/repo"
  DATA_ROOT: "/repo/sub-folder-1"
```

Docker applies nested mounts parent-first whatever order they are listed in.
Inside the container, `sub-folder-2` and every top-level file are on a
read-only mount. **No process in the container** can write them: not git, not
the API, not an attacker with code execution. `.git` and the scope are the
only writable paths. On unRAID, this is three *Path* entries in the
template, with the first set to *Read Only*.

What this costs, stated plainly against `README.md`'s "Security model":

- **Layer 1 used to make the rest of the repository *absent*.** Now it's
  *read-only* instead. A bug in the path layer (layer 2) could now let the
  agent **read** `sub-folder-2` through the file tools, not just see its names
  through `git_status`. You've said that's acceptable.
- **`.git` is now in the namespace and writable**, because git needs it. A
  layer-2 bug that let a file tool write `/repo/.git/config` or
  `/repo/.git/hooks/*` would mean code execution through the next git call.
  Layer 2 alone is the barrier here. The existing `app-only` test
  configuration was built to prove exactly that layer 2 holds on its own (see
  [Tests](#tests) for the `.git`-specific escape cases added to it).

If the repo root is mounted **read-write** instead (a single
`my-repo:/repo` mount), git mode still works and every software check below
still applies. But the write boundary is then enforced by code only.
Startup detects this (`statvfs(GIT_REPO_ROOT).f_flag & ST_RDONLY`), logs a
warning, and reports `"write_boundary": "software"` instead of `"kernel"`
in `get_folder_info` (open question 6 asks whether this should be a refusal
instead).

### How each write tool stays inside the scope

The ro mount is the backstop. The real enforcement is a **pre-check** in each
write tool that computes exactly what git would change and refuses before git
changes anything. Without it, a pull that touched `sub-folder-2` would fail
halfway with `EROFS` and leave a half-updated tree. With it, that never gets
attempted.

| Tool | What could leak out of scope | Pre-check |
|---|---|---|
| `git_commit` | Changes someone **staged on the host** in `sub-folder-2` would be included by a plain `git commit` | Build the commit with a **temporary index** that only scope paths are added to, then verify `diff-tree parent new` ⊆ scope **before** moving `HEAD` (see [`git_commit`](#git_commit)) |
| `git_pull`, `git_merge` | Incoming commits that change `sub-folder-2` would rewrite those files on the host | Compute the result **without touching the tree** (`git merge-tree --write-tree`, or `diff HEAD..target` for fast-forward), and refuse with `outside_scope` if any changed path is outside scope |
| `git_push` | Commits made **on the host** that touch `sub-folder-2` and haven't been pushed would be published by the agent | Every commit in `<remote>/<branch>..HEAD` must change only scope paths (rule in [`git_push`](#git_push)); otherwise refuse with `outside_scope_commits` |
| `git_fetch` | Nothing: only `refs/remotes/*` change | — |

Path comparison is done on git's own NUL-separated (`-z`) repo-relative
output, against the scope prefix with a trailing slash, so `sub-folder-10/`
doesn't match `sub-folder-1/`. Renames count as both paths: a file moved
from `sub-folder-2` into scope touches `sub-folder-2`.

**The trade-off this makes, deliberately.** If someone pushes changes to
`sub-folder-2` upstream, the agent **can't pull them**: that would write
outside its folder. Until someone pulls on the host, the agent's own pushes
will be rejected as non-fast-forward. The refusal message says exactly this
and names the incoming paths, so the agent can tell the user "pull on the
host, then I can push". Open question 2 is whether to offer an opt-in that
lets the agent bring in *remote-authored* content outside its folder (which
also needs a read-write root mount).

---

## Threat model: what must not happen

The agent can already read and write every file in its scope. Git mode must
not let it get more than these: the read-only view of the rest of the
repository described above, and the ability to publish its own scope's
changes to the configured remote. In particular it must not get:

1. **Code execution in the container.** This one matters most. Git
   runs programs from many places: `.git/hooks/*`, `core.hooksPath`,
   `core.fsmonitor`, `core.sshCommand`, `core.pager`, `core.editor`,
   `credential.helper`, `filter.*.clean/smudge`, `diff.*.textconv`,
   `diff.external`, `merge.*.driver`, `remote.*.uploadpack/receivepack`,
   `ext::` transports, and others. If the agent can write any of them, a
   commit or pull becomes `exec`.
2. **A write outside the scope**, on disk or in published history. See
   [The scope model](#the-scope-model).
3. **The deploy key or token.** It's mounted outside both `/repo` and
   `DATA_ROOT`, so no file tool can name it, and no git output may echo it.
   It now lives in the agent-facing process, not a separate container (see
   "Considered and not chosen", B), so use a **deploy key scoped to this one
   repository**, never a user key or a broad PAT.
4. **Pushes or fetches anywhere other than the configured remote.** If the
   agent could pass a URL or remote name, the server would become an
   outbound-request proxy holding a credential.
5. **History destruction.** No force push, no branch deletion, no
   `reset --hard`, no rewriting of commits that are already pushed.
6. **A half-merged working tree left behind.** Conflicts are detected
   before the tree is touched, so a conflicted merge is refused, not aborted
   halfway.
7. **Option injection.** No value the agent supplies may reach git's argv in
   a position where it parses as an option.

### Invariants enforced at startup and on every call

| # | Invariant | How it's enforced |
|---|---|---|
| I1 | `.git` is in `DENIED_NAMES` | Startup refuses git mode otherwise. In scope-`.` mode this is what keeps the file tools out of `.git`. In subfolder mode `.git` isn't under `DATA_ROOT` anyway, but I2 still needs it. |
| I2 | A nested `.git` can never be created inside the scope | Follows from I1, because denied names apply **at any depth**. Otherwise the agent could write `sub-folder-1/x/.git/config` with a `core.fsmonitor`. Git is also never run with discovery enabled (I4). |
| I3 | No git config the agent can write is ever read | Startup runs `git config --list --show-origin --includes` and refuses if any origin is in the working tree outside `.git/`. This catches `[include] path = ../team.gitconfig`. Global and system config are replaced by a server-generated file (I5). |
| I4 | Git never discovers a repository | Every call sets `GIT_DIR=$GIT_REPO_ROOT/.git`, `GIT_WORK_TREE=$GIT_REPO_ROOT`, and `GIT_CEILING_DIRECTORIES=/`. Startup refuses if `.git` is a file (a gitlink pointing elsewhere) rather than a directory. |
| I5 | A fixed environment and fixed `-c` overrides on every call | See [Execution](#execution). The process environment is **not** inherited. |
| I6 | The agent never chooses a remote, URL, refspec, or flag | Remote and branch come only from `GIT_REMOTE` / `GIT_BRANCH`. The only ref an agent ever supplies is `git_merge`'s, which is validated and placed after `--end-of-options`. |
| I7 | No force, delete, reset, or checkout paths exist in the code | Nothing in the module builds `--force`, `+refs` on push, `--delete`, `reset`, `clean`, `checkout`, or `restore`. A test greps the module source for these strings. |
| I8 | No ref moves until the scope check passes | `git_commit` verifies its tree before `update-ref`. `git_pull` / `git_merge` verify the computed result before running the real merge. `git_push` verifies the range before `git push`. A test proves the order by making each check fail and asserting `HEAD`, the tree, and the remote are all unchanged. |

`.gitattributes` inside the scope stays agent-writable, as intended. It
can *select* a filter or merge driver by name, but the command behind that
name only comes from config, and I1, I3 and I5 make config host-controlled.
It also only applies to paths at or below its own directory, which are
inside the scope.

---

## Execution

All git operations live in a new module, `app/git_ops.py`. It knows nothing
about MCP or HTTP; it is pure functions that return dicts or raise
`GitOpError(code, message, detail)`. `app/mcp_endpoint.py` and `app/main.py`
are thin front ends onto it, the way both are front ends onto `Vault` today.

### Process invocation

```python
subprocess.run(
    [GIT, *FIXED_C_FLAGS, subcommand, *fixed_args, "--end-of-options", *agent_args],
    env=GIT_ENV, cwd=GIT_REPO_ROOT, stdin=subprocess.DEVNULL,
    capture_output=True, timeout=..., start_new_session=True,
)
```

- `GIT` is an absolute path resolved once at startup (`/usr/bin/git`). There is
  never a shell, and `PATH` is never looked up per call.
- `start_new_session=True` puts git and its `ssh` child in their own process
  group, so a timeout kills the group (`os.killpg`) and doesn't orphan `ssh`.
- Requires git ≥ 2.38 for `merge-tree --write-tree`. Debian bookworm, the
  `python:3.12-slim` base, ships 2.39. Startup checks the version.

### `GIT_ENV`: built from scratch, never inherited

```
PATH=/usr/bin:/bin
HOME=/tmp
LANG=C.UTF-8  LC_ALL=C.UTF-8           # stable, parseable messages
GIT_DIR=$GIT_REPO_ROOT/.git  GIT_WORK_TREE=$GIT_REPO_ROOT  GIT_CEILING_DIRECTORIES=/
GIT_CONFIG_NOSYSTEM=1
GIT_CONFIG_GLOBAL=/tmp/claude-files-git/gitconfig    # generated at startup, 0600
GIT_TERMINAL_PROMPT=0                  # a missing credential fails, never hangs
GIT_ASKPASS=/bin/false  SSH_ASKPASS=/bin/false
GIT_EDITOR=/bin/true  GIT_PAGER=cat
GIT_SSH_COMMAND=ssh -i /tmp/claude-files-git/id_key -o IdentitiesOnly=yes \
                -o BatchMode=yes -o StrictHostKeyChecking=yes \
                -o UserKnownHostsFile=/tmp/claude-files-git/known_hosts
GIT_AUTHOR_NAME / _EMAIL, GIT_COMMITTER_NAME / _EMAIL    # from config, write ops only
GIT_OPTIONAL_LOCKS=0                   # read ops only
```

The container's own environment holds `API_TOKEN` and possibly `GIT_TOKEN`.
None of it reaches git or ssh except through the names above.

### Fixed `-c` overrides on every call

`-c` beats every config file, so these hold even if the host's
`.git/config` says otherwise:

```
-c core.hooksPath=/dev/null           # unless GIT_RUN_HOOKS=true, see open question 1
-c core.fsmonitor=false
-c core.untrackedCache=false
-c core.pager=cat
-c core.sshCommand=<same as GIT_SSH_COMMAND>
-c credential.helper=                 # an empty first entry resets the list...
-c credential.helper=<server helper>  # ...then only ours (token mode only)
-c protocol.allow=never
-c protocol.https.allow=always
-c protocol.ssh.allow=always          # no file://, git://, ext::
-c submodule.recurse=false
-c fetch.recurseSubmodules=false
-c diff.external=
-c commit.gpgsign=false               # see open question 3
-c gc.auto=0  -c maintenance.auto=false   # no multi-minute gc inside a tool call
-c safe.directory=$GIT_REPO_ROOT      # container uid vs. host owner
```

Diff and log calls also pass `--no-ext-diff --no-textconv --no-color`.

### Credentials

Same variable names as the autocommit sidecar, so an env block can be copied
across:

- `SSH_KEY_PATH`: mounted read-only, staged at startup to
  `/tmp/claude-files-git/id_key` at mode 0600 (ssh rejects a 0644 bind-mount
  key). Includes the same "you mapped a folder, not the file" diagnostic as
  `autocommit.sh:setup_credentials`. The image gains a passwd entry for
  uid 99 for the same reason the sidecar's did: OpenSSH refuses to run for a
  uid with no passwd entry.
- `SSH_KNOWN_HOSTS_PATH`: **required** whenever `SSH_KEY_PATH` is set. The
  sidecar tolerates trust-on-first-use. This container faces the agent and
  holds a deploy key, so it doesn't.
- `GIT_TOKEN`: held in memory only. The server's credential helper is a
  tiny script on the read-only rootfs. It reads the token from a 0600 tmpfs
  file and prints `username=x-access-token` / `password=…`. The token never
  appears in argv (where `ps` would show it) or in remote URLs.

`/tmp` stays `noexec`. Nothing staged there needs exec: the key and
`known_hosts` are data, and the helper lives in the image.

### Output scrubbing

Every stderr/stdout snippet returned to a caller first goes through
`_scrub()`. It removes `user:pass@` and `x-access-token:…@` from URLs, any
literal occurrence of the token, and any line mentioning
`/tmp/claude-files-git/`. Then it truncates to the last 20 lines or 4 KB.

### Timeouts and locking

- `GIT_NETWORK_TIMEOUT_SECONDS` (default `60`) bounds `fetch` and `push`.
  `GIT_LOCAL_TIMEOUT_SECONDS` (default `30`) bounds everything else.
- **Network and worktree steps are never one process.** `git_pull` is run
  as `git fetch` (network, timed, no index lock), then the scope pre-check,
  then a separate local merge (no network). A timeout can only ever kill a
  process that holds no `index.lock`, so a killed pull never wedges the next
  commit. `git_push` takes no index lock either.
- **`GIT_LOCK`:** one process-wide `threading.Lock` that serialises all git
  write tools. Read tools don't take it. The image runs a single uvicorn
  worker (see the comment above `_SESSIONS` in `app/mcp_endpoint.py`), so a
  process-wide lock is enough. The same "don't add workers" warning applies.
- **`WORKTREE_GATE`:** a reader–writer gate between file writes and git.
  Every `Vault` mutation holds it **shared**: `write_file`, `commit_write`,
  `delete_file`, `move_file`, `copy_file`, `make_directory`, and the
  upload-ticket `PUT`. `git_commit` and the merge step of `git_pull` /
  `git_merge` hold it **exclusive**. A file write that arrives during an
  exclusive hold waits up to 10 s, then fails `423 git_busy` ("a git
  operation is updating the folder; retry in a few seconds"). It never
  interleaves with a merge, and a commit never captures a file mid-update.
- If a process **outside** this server holds `index.lock` (a human running
  git on the host), the call fails with `code: "locked"` after one retry 2 s
  later. The lock is never deleted.

---

## Interaction with existing features

### In-flight uploads

A chunked `begin_write` session holds an open directory fd and a
`.upload-*.tmp` file. Upload tickets reserve a path. A pull or merge can
delete or rename the directory underneath either one, and `WORKTREE_GATE`
can't help, because those sessions span several calls.

- `git_pull` and `git_merge` **refuse** with `code: "uploads_in_flight"`
  while any `begin_write` session is open or any upload ticket is pending,
  and list them. The agent's fix is `commit_write`/`abort_write`, or waiting
  for the ticket TTL.
- `git_commit` always excludes `.upload-*.tmp` at any depth, as
  `autocommit.sh:pathspec()` does, so a half-written upload is never
  committed. `git_status` hides them.

### `Vault` and the file tools

Nothing changes. Files that git checks out into the scope are subject to the
same containment as anything else. A symlink pulled from the remote is still
refused by `O_NOFOLLOW`, and a pulled `.ssh/` directory is still a denied
name. Git creates fresh inodes on checkout, so the hard-link check isn't
tripped.

### The autocommit sidecar

It isn't part of this design. With git mode, the agent commits, pulls and
pushes explicitly. If the sidecar keeps running against the same repository
while `ENABLE_GIT_WRITE` is on, the two race on `index.lock`, and the agent's
picture of what has been pushed goes stale. The README will say to stop the
sidecar for any repository where `ENABLE_GIT_WRITE` is on. With only
`ENABLE_GIT_READ`, the two are compatible.

---

## Tool specifications

### Paths in and out

- **Inputs** (`git_commit.paths`, `git_diff.path`) are **scope-relative**,
  exactly like every file tool's `path`. `notes/a.md` means
  `sub-folder-1/notes/a.md`. They are validated with `VAULT.components()`
  (so `..`, absolute paths, and denied names fail exactly as on
  `write_file`), then prefixed with the scope and passed as
  `:(literal)<path>` after `--`, so `*` and `:(…)` pathspec magic can't widen
  them.
- **Outputs** carry both forms: `path` (repo-relative, as git reports it)
  and `scope_path` (scope-relative, or `null` when outside the scope), plus
  `in_scope: bool`. The agent can then pass `scope_path` straight to
  `read_file`, and can see at a glance which changes it can't commit.

### Common result fields

Every git tool's successful result includes a `branch` summary computed after
the operation:

```json
"branch": {
  "name": "main",
  "head": "3f9c2e1",
  "upstream": "origin/main",
  "ahead": 0,
  "behind": 0,
  "as_of_fetch": "2026-11-04T12:31:07Z",
  "operation_in_progress": null
}
```

`ahead`/`behind` are only as fresh as the last fetch, and `as_of_fetch` (the
mtime of `FETCH_HEAD`, or `null`) is there so tool descriptions can tell
agents not to read a stale `behind: 0` as "up to date".

Every failure is raised as `ValueError(f"{code}: {message}")` through
`_guard()` (`app/mcp_endpoint.py:230`), so it's logged as `denied MCP git_* …`
like every other refusal. REST returns
`{"code": ..., "message": ..., "detail": {...}}` with these statuses:

| `code` | HTTP | Meaning / what the message tells the agent |
|---|---|---|
| `readonly` | 403 | Write tool, read-only token. Same `READONLY_MESSAGE` as today. |
| `git_disabled` | 404 | REST only. MCP never sees it, because disabled tools aren't registered. |
| `outside_scope` | 409 | A pull/merge would change files outside the folder. Lists them. "Ask the user to pull on the host." |
| `outside_scope_commits` | 409 | A push would publish commits that touch files outside the folder. Lists commits and paths. "These were committed outside claude-files; push them from the host." |
| `nothing_to_commit` | 409 | No changes in scope. Says how many out-of-scope changes exist, if any, so "but status shows changes" is explained. |
| `too_many_changes` | 409 | Over `GIT_MAX_CHANGED_FILES`. |
| `non_fast_forward` | 409 | Push rejected. "Run `git_pull`, then push again." |
| `diverged` | 409 | `ff-only` impossible. Names `git_pull(strategy="merge")` / `git_merge`. |
| `conflict` | 409 | Merge would conflict. **Nothing was changed.** Lists conflicted paths. |
| `dirty_worktree` | 409 | Incoming changes touch uncommitted files in scope. Lists them; "`git_commit` first". |
| `uploads_in_flight` | 409 | See [In-flight uploads](#in-flight-uploads). |
| `locked` / `git_busy` | 423 | See [Timeouts and locking](#timeouts-and-locking). |
| `invalid_ref` | 400 | `git_merge` ref failed validation or doesn't resolve. |
| `auth_failed` | 502 | Remote refused the credential. Scrubbed stderr tail. |
| `network` | 502 | DNS, connection refused, host key mismatch. Scrubbed stderr tail. |
| `timeout` | 504 | Process group killed. Nothing was left locked. |
| `git_failed` | 500 | Anything unclassified. Scrubbed stderr tail. |

Classification matches English stderr text, which is why `LC_ALL` is pinned.
`autocommit.sh` uses the same tactic to tell non-fast-forward from an auth
failure.

### `git_status`

```python
git_status(ctx, fetch: bool = False, limit: int = 500) -> dict
```

```json
{
  "branch": { ... },
  "scope": "sub-folder-1/",
  "changes": [
    {"path": "sub-folder-1/notes/a.md", "scope_path": "notes/a.md", "in_scope": true,
     "index": "M", "worktree": " "},
    {"path": "sub-folder-2/draft.md", "scope_path": null, "in_scope": false,
     "index": "?", "worktree": "?"}
  ],
  "summary": {"in_scope": 1, "outside_scope": 1, "conflicted": 0},
  "truncated": false
}
```

- Parses `git status --porcelain=v2 --branch -z --untracked-files=all`
  across the **whole repository**. This is the deliberate read across the
  scope boundary. Renames carry `from`/`from_scope_path` too.
- `.upload-*.tmp` files are hidden.
- `limit` caps `changes`. In-scope entries are listed first, so truncation
  drops outside-scope noise before the agent's own files. `summary` always
  has the full counts.
- `fetch=true` runs `git_fetch` first. That needs `ENABLE_GIT_WRITE` and an
  rw token; otherwise it's refused with a message naming which one is missing.

### `git_log`

```python
git_log(ctx, limit: int = 20, scope_only: bool = False) -> dict
```

Returns `{"commits": [{"hash", "short", "author", "date", "subject", "touches_outside_scope"}], "branch": ...}`
for `HEAD`. `limit` is capped at 200. There's no ref argument in v1.
`scope_only=true` adds `-- <scope>`. Subjects only: bodies aren't returned.

### `git_diff`

```python
git_diff(ctx, staged: bool = False, path: str | None = None,
         offset: int = 0, length: int | None = None) -> dict
```

- `git diff [--cached] --no-ext-diff --no-textconv --no-color -- <scope or path>`.
- **Scope only.** `path` is scope-relative and validated as above. With no
  `path`, the whole scope is diffed. `git_status` already discloses *names*
  outside the scope. Showing the *contents* of `sub-folder-2` is a bigger
  read than you've signed off on, so v1 doesn't (open question 5).
- Same truncation contract as `read_file`'s text path:
  `MCP_INLINE_LIMIT_CHARS`, with `offset`/`length` paging and
  `{truncated, total_chars, note}`.

### `git_commit`

```python
git_commit(ctx, message: str, paths: list[str] | None = None) -> dict
```

```json
{"ok": true, "commit": "3f9c2e1…", "files_changed": 4, "insertions": 120,
 "deletions": 8, "outside_scope_changes_left": 2, "branch": { ... }}
```

The commit is built with plumbing against a **temporary index**, so that
anything staged in the real index outside scope (a human's `git add` in
`sub-folder-2`) can't ride along:

1. Take `GIT_LOCK`, then `WORKTREE_GATE` exclusive.
2. Refuse if `HEAD` is detached, isn't `GIT_BRANCH`, or a merge/rebase is
   in progress (that could only come from the host).
3. `GIT_INDEX_FILE=/tmp/claude-files-git/index.<rand>`, then
   `git read-tree HEAD`.
4. `git add -A -- <pathspecs>`, where the pathspecs are the scope (or each
   validated `path` under it) plus `:(exclude,glob)<scope>**/.upload-*.tmp`.
5. Runaway guard: more than `GIT_MAX_CHANGED_FILES` (default `1000`) staged
   → refuse with `too_many_changes`. Nothing has been touched yet: the temp
   index is discarded.
6. `tree=$(git write-tree)`. If it equals `HEAD^{tree}`, the result is
   `nothing_to_commit`.
7. **Scope check (I8):** `git diff-tree -r -z --name-only HEAD $tree`. Every
   path must be under the scope. This can't fail given step 4. It exists so
   a future bug in step 4 fails closed, not open.
8. `commit=$(git commit-tree $tree -p HEAD -F -)` with the message on stdin.
9. `git update-ref refs/heads/<GIT_BRANCH> $commit <old HEAD>`. This is a
   compare-and-swap, so if someone committed on the host meanwhile, it fails
   (`locked`, retry) instead of silently dropping their commit.
10. Bring the real index's scope entries up to date (`git add -A -- <scope>`
    on the real index, same exclusions) so `git_status` shows the scope
    clean. Real-index entries outside scope are left exactly as the human
    left them.

Message rules: required, stripped, 1–5000 characters, no NUL bytes. It's
passed on stdin, so a message beginning with `-` is just a message. If
`GIT_COMMIT_TRAILER` is set (e.g. `Committed-Via: claude-files`), it's
appended. Author and committer come only from `GIT_AUTHOR_NAME` /
`GIT_AUTHOR_EMAIL`; there's no argument for them. Plumbing doesn't run
`pre-commit`/`commit-msg` hooks, which is consistent with hooks being off.

### `git_push`

```python
git_push(ctx) -> dict
```

No arguments.

1. Refuse if `HEAD` is detached or isn't `GIT_BRANCH` (re-checked every
   call, since the host can `git checkout`).
2. **Scope check (I8)** over `R = <GIT_REMOTE>/<GIT_BRANCH>..HEAD`
   (remote-tracking ref as of the last fetch):
   - each **non-merge** commit in `R`: `diff-tree <parent> <commit>` ⊆ scope;
   - each **merge** commit in `R`: for at least one parent,
     `diff-tree <parent> <commit>` ⊆ scope.

   By induction, every out-of-scope change in what's pushed is already on the
   remote. The agent never publishes a host-made change to `sub-folder-2`,
   even one that was later reverted (so a net-diff check alone isn't enough).
   Failures → `outside_scope_commits` with `[{commit, subject, paths}]`.
3. `git push --porcelain <GIT_REMOTE> HEAD:refs/heads/<GIT_BRANCH>`.

```json
{"ok": true, "pushed": "3f9c2e1..a71b004", "commits": 2, "branch": { ... }}
```

- Already up to date: `{"ok": true, "pushed": null, "commits": 0}`.
- Non-fast-forward: `non_fast_forward`, naming `git_pull`. **It doesn't
  rebase and retry by itself.** A silent rebase would change what the agent
  believes it pushed.
- If the remote-tracking ref is stale, step 2's range may be too wide (it
  includes commits the remote actually has). That's safe: it can only cause
  a spurious refusal, never a wrong push. The refusal suggests `git_fetch`.

### `git_pull`

```python
git_pull(ctx, strategy: Literal["ff-only", "merge", "rebase"] = "ff-only") -> dict
```

```json
{"ok": true, "strategy": "ff-only", "updated": "a71b004..c02d9e3",
 "files_changed": ["notes/a.md", "src/x.py"], "branch": { ... }}
```

1. Refuse with `uploads_in_flight` if any are open.
2. `git fetch --no-tags <GIT_REMOTE> +refs/heads/<GIT_BRANCH>:refs/remotes/<GIT_REMOTE>/<GIT_BRANCH>`
   (network step, timed). `T` = that ref.
3. **Scope check (I8)**, with nothing touched yet:
   - `ff-only`: `HEAD` must be an ancestor of `T`, else `diverged`. Then
     `diff-tree HEAD T` ⊆ scope.
   - `merge`: `git merge-tree --write-tree -z HEAD T` computes the merge
     result and any conflicts **without touching the index or tree**.
     Conflicts → `conflict` (nothing changed). Otherwise
     `diff-tree HEAD <result>` ⊆ scope.
   - `rebase`: `diff-tree $(merge-base HEAD T) T` ⊆ scope (what upstream
     changed), and a dry `merge-tree` of each local commit onto `T` must be
     conflict-free. Only commits in `T..HEAD` are rebased, and those are by
     definition unpushed.

   Any path outside scope → `outside_scope` with the list.
4. Take `WORKTREE_GATE` exclusive. Run the real `git merge --ff-only T` /
   `git merge --no-edit T` / `git rebase T` (no network). In-scope dirty
   files that incoming changes would overwrite make git refuse before
   changing anything, and that's reported as `dirty_worktree`. If anything
   unexpected still fails partway, `merge --abort` / `rebase --abort` runs
   and the result is `git_failed` with the tree restored.

`--autostash` is **not** used. A stash that fails to re-apply leaves conflict
markers in the tree, which breaks guarantee 6. `files_changed` is
scope-relative (in strict scope that's all of them) and capped at 500 with
`truncated`.

### `git_merge`

```python
git_merge(ctx, ref: str, no_ff: bool = False) -> dict
```

Merges `ref` into the current branch. It uses the same steps 1, 3 (`merge`),
and 4 as `git_pull`, with the same results.

`ref` validation (I6), in order:

1. 1–255 characters, no NUL, no whitespace, doesn't start with `-`.
2. Either `git check-ref-format --branch <ref>` succeeds (a local branch), or
   `ref` is `<GIT_REMOTE>/<name>` and `refs/remotes/<GIT_REMOTE>/<name>`
   passes `check-ref-format`.
3. The full refname, under `refs/heads/` or `refs/remotes/<GIT_REMOTE>/`
   only, resolves with `rev-parse --verify --quiet --end-of-options
   <full>^{commit}`. Raw SHAs, tags, `HEAD@{…}`, `:/text` and other revision
   syntax are refused in v1.
4. Only the **resolved full refname** goes into argv, after
   `--end-of-options`. The agent's string never does.

Merging the current branch into itself is refused.

### `git_fetch`

```python
git_fetch(ctx) -> dict
```

Step 2 of `git_pull` on its own: `{"ok": true, "updated_refs": [...], "branch": { ... }}`.
It lets an agent see `behind` before choosing a strategy. It also refreshes
the range that `git_push`'s scope check uses.

---

## `get_folder_info` additions

```json
"git": {
  "read_enabled": true,
  "write_enabled": true,
  "scope": "sub-folder-1/",
  "write_boundary": "kernel",
  "remote": "origin",
  "branch": "main",
  "hooks": "disabled",
  "max_changed_files": 1000,
  "note": "Your folder is sub-folder-1/ inside a larger repository. git_status shows changes across the whole repository; you can only commit, pull, merge or push changes inside your folder, and those tools refuse (outside_scope / outside_scope_commits) rather than touch anything else. Push is never forced; on non_fast_forward, git_pull then git_push. Conflicts are detected before anything changes. ahead/behind are only as fresh as the last fetch. Nothing is committed or pushed automatically: call git_commit and git_push yourself."
}
```

With both switches off, this is
`{"read_enabled": false, "write_enabled": false, "note": "Git tools are disabled on this server (ENABLE_GIT_READ / ENABLE_GIT_WRITE)."}`,
so an agent asked to "push this" can tell the user why it can't. The remote
URL is never reported: it can carry a username, and the agent doesn't need it.
The git read tools join the ro-token list in `token_scope_note`.

The Claude_Files connector's server instructions currently say "Anything
written through these tools is committed and pushed automatically". That
becomes untrue on a server running git mode. The sentence should be
conditional: when `ENABLE_GIT_WRITE` is on, say instead that the agent
commits and pushes with the git tools.

---

## REST parity

One route per tool, as the connector plan requires. A disabled class's
routes return 404 `git_disabled`.

| Route | Auth | Tool |
|---|---|---|
| `GET  /v1/git/status?fetch=&limit=` | ro (`fetch=true` needs rw) | `git_status` |
| `GET  /v1/git/log?limit=&scope_only=` | ro | `git_log` |
| `GET  /v1/git/diff?staged=&path=&offset=&length=` | ro | `git_diff` |
| `POST /v1/git/commit` `{"message", "paths"?}` | rw | `git_commit` |
| `POST /v1/git/pull` `{"strategy"?}` | rw | `git_pull` |
| `POST /v1/git/push` `{}` | rw | `git_push` |
| `POST /v1/git/merge` `{"ref", "no_ff"?}` | rw | `git_merge` |
| `POST /v1/git/fetch` `{}` | rw | `git_fetch` |

`GET /v1/info` gets the same `git` block. `AGENT.md` gets a short "Git"
section for the REST fallback. The rate limiter applies as usual.

---

## Configuration

New variables in `app/config.py`, `.env.example`, `docker-compose.yml`,
`unraid/claude-files.xml`, and the README's Configuration table:

| Variable | Default | Meaning |
|---|---|---|
| `ENABLE_GIT_READ` | `false` | Register `git_status`, `git_log`, `git_diff`. |
| `ENABLE_GIT_WRITE` | `false` | Register `git_commit`, `git_pull`, `git_push`, `git_merge`, `git_fetch`. |
| `GIT_REPO_ROOT` | `DATA_ROOT` | Where the repository root is mounted. `DATA_ROOT` must be it or under it, and the scope is the difference. |
| `GIT_REMOTE` | `origin` | The only remote ever fetched from or pushed to. |
| `GIT_BRANCH` | branch checked out at startup | The only branch ever committed to or pushed. Re-checked on each write. |
| `GIT_AUTHOR_NAME` | `claude-files agent` | Author and committer for `git_commit`. |
| `GIT_AUTHOR_EMAIL` | `agent@localhost` | 〃 |
| `GIT_COMMIT_TRAILER` | — | Optional trailer appended to every commit message. |
| `GIT_MAX_CHANGED_FILES` | `1000` | `git_commit` runaway guard. `0` disables. |
| `GIT_NETWORK_TIMEOUT_SECONDS` | `60` | Fetch/push timeout. |
| `GIT_LOCAL_TIMEOUT_SECONDS` | `30` | Timeout for every other git call. |
| `GIT_RUN_HOOKS` | `false` | See open question 1. |
| `SSH_KEY_PATH` | — | Deploy key, for ssh remotes. |
| `SSH_KNOWN_HOSTS_PATH` | — | **Required** if `SSH_KEY_PATH` is set. |
| `GIT_TOKEN` | — | HTTPS token instead of a key. |

Remote, branch and credential variables are only validated when
`ENABLE_GIT_WRITE=true`. Read mode needs no credential and never touches the
network.

### Image

A `WITH_GIT` build arg, default `1`, mirroring `WITH_MCP`. It installs `git`
and `openssh-client` (Debian packages, about 35 MB) in the final stage, plus
the uid-99 passwd entry. If the image was built with `WITH_GIT=0`, setting
either `ENABLE_GIT_*` flag to `true` is a startup error that names the build
arg.

### Full compose example (the subfolder layout)

```yaml
volumes:
  - /mnt/user/appdata/repos/my-repo:/repo:ro
  - /mnt/user/appdata/repos/my-repo/.git:/repo/.git
  - /mnt/user/appdata/repos/my-repo/sub-folder-1:/repo/sub-folder-1
  - /mnt/user/appdata/keys/deploy_ed25519:/keys/id_ed25519:ro
  - /mnt/user/appdata/keys/known_hosts:/keys/known_hosts:ro
environment:
  DATA_ROOT: "/repo/sub-folder-1"
  GIT_REPO_ROOT: "/repo"
  DENIED_NAMES: ".git,.gitmodules,.ssh"     # .git is mandatory in git mode
  ENABLE_GIT_READ: "true"
  ENABLE_GIT_WRITE: "true"
  GIT_REMOTE: "origin"
  GIT_BRANCH: "main"
  SSH_KEY_PATH: "/keys/id_ed25519"
  SSH_KNOWN_HOSTS_PATH: "/keys/known_hosts"
```

The existing `REQUIRE_MOUNT` check (`app/main.py:65`) passes unchanged,
because `DATA_ROOT` is its own bind mount.

---

## Startup validation

When either switch is on, fail loudly at startup, the way `autocommit.sh`'s
`validate()` and the mount check at `app/main.py:65` do. Each failure is a
`RuntimeError` whose message says how to fix it.

Either switch:

1. The image has git (`WITH_GIT=1`), and its version is ≥ 2.38.
2. `.git` ∈ `DENIED_NAMES` (I1).
3. `$GIT_REPO_ROOT/.git` is a directory, not a file or symlink (I4).
4. `git rev-parse --show-toplevel` equals `GIT_REPO_ROOT`.
5. `DATA_ROOT` is `GIT_REPO_ROOT` or a descendant, and the path between them
   has no symlink component (walked with `O_NOFOLLOW`). The derived scope
   isn't inside `.git`.
6. No config origin inside the working tree (I3).
7. `HEAD` is not detached.
8. Write boundary: if the scope isn't `.` and `GIT_REPO_ROOT`'s mount is
   writable, log a WARNING and report `write_boundary: "software"`.

Write mode only:

9. `GIT_REMOTE` exists, and its URL is `ssh://`, scp-style `user@host:path`,
   or `https://`. `file://`, a local path, and `ext::` are refused here as
   well as by `protocol.allow`.
10. `GIT_BRANCH`, if set, equals the checked-out branch.
11. `$GIT_REPO_ROOT/.git` and `DATA_ROOT` are writable (a probe with
    `os.access` and `statvfs`), so a misconfigured `:ro` on the wrong mount
    fails at startup, not on the first commit.
12. If `SSH_KEY_PATH` is set: it's a file, it stages to 0600, and
    `SSH_KNOWN_HOSTS_PATH` is set and is a file.
13. If `ENABLE_GIT_READ=false`: log the warning described under
    [Why read and write are split this way](#why-read-and-write-are-split-this-way).

Startup doesn't contact the remote. The first write call surfaces
`auth_failed` / `network` if the credential is wrong.

---

## Tests

A new `tests/git_suite.py`, driven by `tests/run_tests.sh`. The fixture is a
repository with `sub-folder-1/` and `sub-folder-2/`, mounted with the
three-mount layout, with a bare-repo "remote" served over **ssh** by a
throwaway `sshd` container. `file://` is refused by design, so it can't be
the test transport. A second, smaller pass runs the scope-`.` layout.

**Functional**
- status: whole-repo listing with correct `in_scope`; in-scope entries
  sorted first under `limit`; renames across the boundary; `.upload-*.tmp`
  hidden
- commit: all, explicit `paths`, deletion, `nothing_to_commit` (reporting
  out-of-scope counts), `too_many_changes` (and nothing left behind), message
  starting with `-`, trailer, fixed author, CAS failure when the host commits
  concurrently
- push: fast-forward, up-to-date no-op, `non_fast_forward`
- pull: ff-only, `diverged`, merge (clean), merge (conflict → `conflict` and
  tree/index/HEAD **byte-identical** to before), rebase, `dirty_worktree`
- merge: local branch, `origin/feature`, conflict, `no_ff`
- `uploads_in_flight`; `git_busy` (a `write_file` during an artificially
  slowed merge waits, then succeeds)
- tool registration and `get_folder_info.git` in all four on/off
  combinations; ro token → read tools work, write tools `readonly`,
  `status(fetch=true)` refused

**Scope: the write boundary**
- Host edits **and stages** `sub-folder-2/x`. `git_commit` → commit contains
  only `sub-folder-1` paths, and `sub-folder-2/x` is still staged in the real
  index afterwards.
- Upstream commit touches `sub-folder-2` → `git_pull` (each strategy) and
  `git_merge` → `outside_scope`, and nothing changed on disk, in the index, or
  in `HEAD`.
- Host makes a local commit touching `sub-folder-2` → `git_push` →
  `outside_scope_commits`, and the remote is unchanged. Same with a
  commit-then-revert pair (net diff empty), to prove per-commit checking.
- A merge commit whose out-of-scope content comes only from the published
  parent → push allowed.
- `sub-folder-10/` sibling: not treated as in scope.
- Every file tool and REST route with `../sub-folder-2/x`, encoded variants,
  and the absolute `/repo/sub-folder-2/x`: refused, canary unchanged.
- With the `:ro` root mount: a `docker exec` write to
  `/repo/sub-folder-2` fails with `EROFS` (proves `write_boundary: "kernel"`).
  With a writable root: warning logged, `write_boundary: "software"`.

**Containment: git as an escape route**
- File tools on `.git/…`, `x/.git/config` inside the scope: refused. In
  subfolder mode, also `../.git/config`.
- Host-planted executable `.git/hooks/{pre-commit,post-merge,pre-push,reference-transaction}`
  that touch a canary: canary never appears.
- Host `.git/config` with `core.fsmonitor`, `core.pager`, `core.sshCommand`,
  `diff.external` pointing at a canary script: never runs.
- `[include] path = ../sub-folder-1/x.gitconfig`: startup refused.
- An agent-written `.gitattributes` naming an undefined filter/diff/merge
  driver: no execution.
- `git_merge` refs: `--upload-pack=touch /tmp/x`, `-X ours`, `HEAD@{1}`,
  `:/foo`, a raw SHA, `../x`, `refs/tags/v1`, `other-remote/main`, 300
  characters, embedded NUL: each → `invalid_ref`.
- `git_diff` / `git_commit` paths: `..`, `.git/HEAD`, `*`, `:(top)`,
  absolute: each refused before git runs.
- Startup refusals: `.git` not denied, `.git` is a gitlink file, `DATA_ROOT`
  outside `GIT_REPO_ROOT`, a symlink between them, a `file://` remote,
  `SSH_KEY_PATH` without `SSH_KNOWN_HOSTS_PATH`, `ENABLE_GIT_READ=true` on a
  `WITH_GIT=0` image.
- Token scrubbing: `GIT_TOKEN=ghp_CANARY…` with a wrong remote. The canary is
  absent from MCP results, REST bodies, and `docker logs`.
- Timeout: a remote that accepts and then stalls. `git_push` → `timeout`
  within the bound, no `ssh` left (`docker top`), no `index.lock`, and the
  next `git_commit` succeeds.
- Source grep (I7) on `app/git_ops.py`.

CI: add `tests/git_suite.py` to the `quick-checks` byte-compile list, and run
it in the `containment-suite` job against the `WITH_GIT=1, WITH_MCP=1` build.

---

## Phasing

One PR per phase, each shippable on its own:

1. **Foundations + push/pull.** `app/git_ops.py` (execution, env, `-c` flags,
   scrubbing, locks, startup validation, the scope engine), both switches,
   `GIT_REPO_ROOT`, `git_status`, `git_commit`, `git_pull` (`ff-only`),
   `git_push`, `git_fetch`, the `get_folder_info`/`/v1/info` block, REST
   routes, `WITH_GIT`, and **all of the scope and containment tests**. Those
   tests land with the first tool that runs git, not after it.
2. **Merge.** `git_merge`, `git_pull(strategy="merge" | "rebase")`, and the
   conflict and `invalid_ref` tests.
3. **Inspection.** `git_log`, `git_diff`.

README (a new "Git mode" section under "MCP connector", a rewritten
Security model paragraph for the read-only-root layout, the sidecar note) and
`AGENT.md` ship with phase 1 and grow with each phase.

---

## Considered and not chosen

**A. A generic `git(args: list[str])` tool with an allow-list.** It's one
tool, but argv allow-listing for git is a losing game: `-c`,
`--upload-pack`, `--exec`, `-C`, `--git-dir`, `--output`, pathspec magic,
and per-subcommand options that execute things, with more every release. A
fixed operation per tool, where the agent supplies at most one validated ref,
is auditable in one sitting, which is this repo's stated bar.

**B. Run git in a second container and have the API call it.** This would
keep the deploy key out of the agent-facing process. Rejected: you want one
container, and the sidecar's track record argues for fewer moving parts.
`app/git_ops.py`'s pure-function boundary is the seam if this is ever
revisited.

**C. Scope by sparse checkout.** A sparse checkout of `sub-folder-1/` would
hide `sub-folder-2` from git altogether. Rejected: the host and the
container share one working tree, so it would change what the human sees
too, and it would remove the cross-scope `git_status` visibility you asked
for.

**D. Auto-rebase-and-retry inside `git_push`, and `--autostash` on pull.**
Rejected: the agent should see divergence, not have it papered over, and a
failed autostash pop leaves conflict markers behind.

**E. Built-in background autocommit.** It was drafted and then dropped at
your request. The agent decides when to commit, pull and push. The existing
sidecar is untouched by this plan.

**F. Finer-grained switches (one per operation).** Two switches cover the
read/write split asked for. The natural extension, if "pull but never push"
is ever wanted, is an optional `GIT_WRITE_OPS` allow-list checked at
registration. It isn't in v1.

---

## Open questions

1. **Hooks.** `GIT_RUN_HOOKS=false` is the default, and this spec recommends
   leaving it that way. A hook that runs repo code (`npm test`,
   `pre-commit run`) executes **agent-written files**, which turns "can write
   a file" into "can execute code in the container". If the option is
   offered at all, startup should log a warning that says exactly that.
2. **Remote-authored changes outside the scope.** In strict mode, upstream
   changes to `sub-folder-2` block the agent's pull, and so its push, until
   someone pulls on the host. An opt-in `GIT_PULL_OUTSIDE_SCOPE=true` could
   allow pulls/merges whose outside-scope changes come **only** from commits
   already on the remote (the agent still can't author them). That requires
   a writable repo-root mount (`write_boundary: "software"`), and it means
   the agent's pull rewrites files a human may be looking at. Off unless you
   want it.
3. **Signed commits.** This spec forces `commit.gpgsign=false`. If the
   remote requires signed commits, every push will be rejected, and that
   needs a signing key in the container (out of scope).
4. **Branch switching.** Deliberately absent: `GIT_BRANCH` pins one branch. A
   workflow where the agent opens `agent/*` branches for a human to merge
   needs branch-name policy and its own document.
5. **`git_diff` outside the scope.** v1 diffs only the scope. You've accepted
   reads across the boundary via `git_status`. Should `git_diff` also show
   *contents* of out-of-scope changes?
6. **Writable repo root: warn or refuse?** v1 warns and reports
   `write_boundary: "software"`. A refusal (with an explicit override) would
   make the kernel boundary non-optional for subfolder scopes.
