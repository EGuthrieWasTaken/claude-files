"""Git tools: status / log / diff / commit / fetch / pull / push / merge.

Design brief: docs/plan-mcp-git-ops.md -- read that before touching this file.

This module knows nothing about MCP or HTTP. It is pure operations that return
dicts or raise ``GitOpError(code, message, detail)``; app/mcp_endpoint.py and
app/main.py are thin front ends onto it, the way both are front ends onto
``Vault``.

The one idea everything here serves: **the agent's folder is a *scope* inside
a repository, and no write may leave it.** Reads (``status``) may cross the
boundary. Every write tool first works out exactly what git *would* change --
a temporary index for a commit, ``merge-tree`` for a pull or merge, a
per-commit range check for a push -- and refuses *before* touching anything if
that reaches outside the scope (invariant I8). The recommended read-only mount
of the repository root is the kernel-enforced backstop; this module is the
real enforcement and does not depend on it.

Git is also the most dangerous thing this container runs, because git executes
programs named in config (hooks, fsmonitor, filters, credential helpers, ssh
commands...). So git is never run with an inherited environment, never with
discovery, never with a config the agent could have written, and with
hooks and every external-program setting forced off on the command line. See
"Threat model" and "Execution" in the plan.
"""

from __future__ import annotations

import atexit
import contextlib
import datetime
import logging
import os
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from typing import Any, Callable, Iterator

from .config import Config
from .gate import GateTimeout

log = logging.getLogger("claude-files")

GIT_CANDIDATES = ("/usr/bin/git", "/usr/local/bin/git", "/bin/git")
# merge-tree --write-tree (the no-touch conflict/scope pre-check) needs 2.38.
MIN_GIT_VERSION = (2, 38)

# A hard cap on what one git call may hand back, so a pathological diff or a
# status over a million files can't eat the container's memory limit.
MAX_OUTPUT_BYTES = 32 * 1024 * 1024
MAX_FILES_LISTED = 500
MAX_PUSH_RANGE = 1000
MAX_MESSAGE_CHARS = 5000

# Test seams ONLY. The production allow-list is ssh and https; a local
# bare-repo remote (file transport) is refused by design, so the tests, which
# cannot run an sshd in-process, widen these on the module. Neither is
# reachable from an environment variable or from an agent.
EXTRA_URL_PATTERNS: list[str] = []
EXTRA_PROTOCOLS: list[str] = []

# Every pattern requires an alphanumeric first character where the user/host
# begins. A leading dash there would reach ssh as an *option* (the classic
# "-oProxyCommand=..." host trick), which is code execution.
_URL_PATTERNS = (
    r"^ssh://[A-Za-z0-9][^\s]*$",
    r"^https://[A-Za-z0-9][^\s]*$",
    # scp-style: user@host:path (no "://", which the first two cover).
    r"^[A-Za-z0-9][A-Za-z0-9._-]*@[A-Za-z0-9][A-Za-z0-9._-]*:[^\s]+$",
)

_REMOTE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
# merge refs: a strict allow-list of characters, so that nothing that git
# would parse as revision syntax (@{, ^, ~, :, ?, *, [, whitespace, ...) can
# even reach check-ref-format.
_MERGE_REF_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
_UPLOAD_TMP_RE = re.compile(r"(^|/)\.upload-[^/]*\.tmp$")

HTTP_STATUS = {
    "readonly": 403,
    "git_disabled": 404,
    "invalid_ref": 400,
    "invalid_argument": 400,
    "outside_scope": 409,
    "outside_scope_commits": 409,
    "nothing_to_commit": 409,
    "too_many_changes": 409,
    "non_fast_forward": 409,
    "diverged": 409,
    "conflict": 409,
    "dirty_worktree": 409,
    "uploads_in_flight": 409,
    "detached_head": 409,
    "wrong_branch": 409,
    "operation_in_progress": 409,
    "stale_remote": 409,
    "locked": 423,
    "git_busy": 423,
    "auth_failed": 502,
    "network": 502,
    "timeout": 504,
    "git_failed": 500,
}


class GitOpError(Exception):
    """A refused or failed git operation. ``code`` is the stable identifier the
    plan's error table documents; ``message`` already tells the agent what to
    do next and names any paths involved."""

    def __init__(self, code: str, message: str, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}

    @property
    def status(self) -> int:
        return HTTP_STATUS.get(self.code, 500)


def _short(sha: str | None) -> str | None:
    return sha[:7] if sha else None


def _listing(paths: list[str], limit: int = 10) -> str:
    shown = ", ".join(paths[:limit])
    more = len(paths) - limit
    return shown + (f" (and {more} more)" if more > 0 else "")


def _escape_glob(text: str) -> str:
    return re.sub(r"([\\*?\[])", r"\\\1", text)


class GitRepo:
    """One repository, one scope, one remote, one branch. Built once at startup."""

    def __init__(
        self,
        cfg: Config,
        *,
        vault: Any,
        gate: Any,
        uploads_in_flight: Callable[[], list[str]] | None = None,
    ) -> None:
        self.cfg = cfg
        self.vault = vault
        self.gate = gate
        self._uploads_in_flight = uploads_in_flight or (lambda: [])
        self.read_enabled = cfg.enable_git_read
        self.write_enabled = cfg.enable_git_write

        self.remote = cfg.git_remote
        self.branch = cfg.git_branch  # filled from HEAD at startup if empty
        self.pull_outside_scope = cfg.git_pull_outside_scope

        repo_abs = os.path.abspath(cfg.git_repo_root or cfg.root)
        data_abs = os.path.abspath(cfg.root)
        self._repo_abs = repo_abs
        self._data_abs = data_abs
        self.repo_root = os.path.realpath(repo_abs)
        self.git_dir = os.path.join(self.repo_root, ".git")
        self.scope_dir = ""   # "" or "sub/folder" (no trailing slash)
        self.scope = ""       # "" or "sub/folder/" (trailing slash, for prefix tests)
        self.write_boundary = "n/a"

        # The one lock every git *write* tool takes. Read tools don't. See
        # "Timeouts and locking" in the plan: the image runs a single uvicorn
        # worker, so a process-wide lock is enough.
        self._lock = threading.Lock()

        self.git_bin = ""
        self._state_dir = ""
        self._c_flags: list[str] = []
        self._ssh_command = ""
        self._started = False

    # ======================================================================
    # Startup
    # ======================================================================

    def start(self) -> None:
        """Validate the deployment and stage credentials. Raises RuntimeError
        with a message that says how to fix it -- the same fail-loudly stance
        as the mount check in app/main.py and autocommit.sh's validate()."""
        try:
            self._start()
        except GitOpError as exc:
            raise RuntimeError(f"git mode: {exc.message}") from exc

    def _start(self) -> None:
        cfg = self.cfg
        self._find_git()
        self._check_version()

        if ".git" not in cfg.denied_names:
            raise RuntimeError(
                "git mode requires '.git' in DENIED_NAMES (the default). Without it a file "
                "tool could write .git/config or .git/hooks and turn the next git call into "
                "code execution."
            )
        self._check_layout()
        self._state_dir = tempfile.mkdtemp(prefix="claude-files-git-")
        os.chmod(self._state_dir, 0o700)
        atexit.register(shutil.rmtree, self._state_dir, ignore_errors=True)
        self._stage_credentials()
        self._build_flags()
        self._check_repository()
        self._check_config_origins()
        self._check_write_boundary()
        if self.write_enabled:
            self._check_write_mode()
            if not self.read_enabled:
                log.warning(
                    "ENABLE_GIT_WRITE is on but ENABLE_GIT_READ is off: the agent can push but "
                    "cannot run git_status. Every write tool still returns a branch summary, "
                    "but this combination is usually a mistake."
                )
        self._started = True
        log.info(
            "git tools: read=%s write=%s scope=%s remote=%s branch=%s boundary=%s",
            self.read_enabled, self.write_enabled, self.scope or ".", self.remote,
            self.branch, self.write_boundary,
        )

    def _find_git(self) -> None:
        for candidate in GIT_CANDIDATES:
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                self.git_bin = candidate
                return
        raise RuntimeError(
            "ENABLE_GIT_READ/ENABLE_GIT_WRITE is set but git is not installed in this image. "
            "Rebuild with WITH_GIT=1 (the default); this image was built with WITH_GIT=0."
        )

    def _check_version(self) -> None:
        out = subprocess.run(
            [self.git_bin, "--version"], capture_output=True, env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"}
        ).stdout.decode(errors="replace")
        m = re.search(r"(\d+)\.(\d+)", out)
        if not m or (int(m.group(1)), int(m.group(2))) < MIN_GIT_VERSION:
            raise RuntimeError(
                f"git >= {MIN_GIT_VERSION[0]}.{MIN_GIT_VERSION[1]} is required "
                f"(merge-tree --write-tree); found: {out.strip() or 'unknown'}"
            )

    def _check_layout(self) -> None:
        repo_abs, data_abs = self._repo_abs, self._data_abs
        try:
            common = os.path.commonpath([repo_abs, data_abs])
        except ValueError:
            common = ""
        if common != repo_abs:
            raise RuntimeError(
                f"DATA_ROOT ({data_abs}) must be GIT_REPO_ROOT ({repo_abs}) or a folder under it."
            )
        rel = os.path.relpath(data_abs, repo_abs)
        if rel != ".":
            # Every component between the repository root and the shared
            # folder must be a real directory: a symlink there would let the
            # derived scope name something other than where the Vault is.
            cur = repo_abs
            for part in rel.split(os.sep):
                cur = os.path.join(cur, part)
                try:
                    st = os.lstat(cur)
                except OSError as exc:
                    raise RuntimeError(f"cannot stat {cur}: {exc.strerror}") from exc
                if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
                    raise RuntimeError(
                        f"{cur} is a symlink or not a directory; the path from GIT_REPO_ROOT "
                        "to DATA_ROOT must be plain directories."
                    )
            if os.path.realpath(data_abs) != os.path.join(self.repo_root, rel):
                raise RuntimeError("DATA_ROOT does not resolve to a folder under GIT_REPO_ROOT.")
            if rel == ".git" or rel.startswith(".git" + os.sep):
                raise RuntimeError("DATA_ROOT must not be inside .git.")
            self.scope_dir = rel.replace(os.sep, "/")
            self.scope = self.scope_dir + "/"
        try:
            st = os.lstat(self.git_dir)
        except OSError as exc:
            raise RuntimeError(
                f"{self.git_dir} does not exist: GIT_REPO_ROOT ({self.repo_root}) is not a git "
                "repository root. Mount the repository root there (see docs/plan-mcp-git-ops.md)."
            ) from exc
        if not stat.S_ISDIR(st.st_mode) or stat.S_ISLNK(st.st_mode):
            raise RuntimeError(
                f"{self.git_dir} must be a real directory, not a file or symlink (a gitlink "
                "would point git somewhere this server has not validated)."
            )

    def _stage_credentials(self) -> None:
        cfg = self.cfg
        sd = self._state_dir
        known_hosts = os.path.join(sd, "known_hosts")
        key = os.path.join(sd, "id_key")
        open(known_hosts, "a").close()
        os.chmod(known_hosts, 0o600)
        have_key = False
        if cfg.ssh_key_path:
            if not self.write_enabled:
                log.info("SSH_KEY_PATH ignored: git write tools are off, nothing touches the network")
            else:
                src = cfg.ssh_key_path
                if os.path.isdir(src):
                    raise RuntimeError(
                        f"SSH_KEY_PATH is a directory, not a file: {src}. You have mapped a FOLDER "
                        "to this path. Either map the key FILE itself there, or map the folder "
                        "elsewhere and point SSH_KEY_PATH at the file inside it."
                    )
                if not os.path.isfile(src):
                    raise RuntimeError(f"SSH_KEY_PATH does not exist: {src} (check the volume mapping)")
                if not cfg.ssh_known_hosts_path:
                    raise RuntimeError(
                        "SSH_KNOWN_HOSTS_PATH is required when SSH_KEY_PATH is set. This container "
                        "faces the agent and holds a deploy key, so it never trusts a host key on "
                        "first use (unlike the autocommit sidecar). Pin the host key."
                    )
                if not os.path.isfile(cfg.ssh_known_hosts_path):
                    raise RuntimeError(
                        f"SSH_KNOWN_HOSTS_PATH is not a file: {cfg.ssh_known_hosts_path}"
                    )
                shutil.copyfile(src, key)
                os.chmod(key, 0o600)
                shutil.copyfile(cfg.ssh_known_hosts_path, known_hosts)
                os.chmod(known_hosts, 0o600)
                have_key = True
        ssh = ["ssh", "-F", "/dev/null", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
               "-o", f"UserKnownHostsFile={known_hosts}"]
        if have_key:
            ssh += ["-i", key, "-o", "IdentitiesOnly=yes"]
        self._ssh_command = " ".join(ssh)

        self._token_file = ""
        if cfg.git_token and self.write_enabled:
            self._token_file = os.path.join(sd, "token")
            fd = os.open(self._token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(cfg.git_token + "\n")

        # A server-generated global config: the only global config git ever
        # sees. Nothing in it names a program.
        self._global_config = os.path.join(sd, "gitconfig")
        with open(self._global_config, "w") as f:
            f.write(f"[safe]\n\tdirectory = {self.repo_root}\n")
        os.chmod(self._global_config, 0o600)

    def _build_flags(self) -> None:
        """Fixed -c overrides on every invocation. -c beats every config file,
        so these hold even if the host's .git/config says otherwise (I5)."""
        protocols = ["https", "ssh", *EXTRA_PROTOCOLS]
        flags = [
            "core.hooksPath=/dev/null",      # hooks never run (plan, Decisions 1)
            "core.fsmonitor=false",
            "core.untrackedCache=false",
            "core.pager=cat",
            "core.editor=true",
            "sequence.editor=true",
            f"core.sshCommand={self._ssh_command}",
            "credential.helper=",            # an empty entry resets the list...
            "protocol.allow=never",
            "submodule.recurse=false",
            "fetch.recurseSubmodules=false",
            "commit.gpgsign=false",
            "tag.gpgsign=false",
            "merge.verifySignatures=false",
            "merge.autostash=false",
            "rebase.autostash=false",
            "pull.rebase=false",
            "rerere.enabled=false",
            "gc.auto=0",
            "maintenance.auto=false",
            "advice.detachedHead=false",
            f"safe.directory={self.repo_root}",
        ]
        for proto in protocols:
            flags.append(f"protocol.{proto}.allow=always")
        if self._token_file:
            # ...then only ours. Git runs this through sh; the token is read
            # from a 0600 file in a 0700 tmpfs dir, never put in argv (ps
            # would show it) or in a remote URL.
            flags.append(
                "credential.helper=!f() { echo username=x-access-token; "
                f"echo \"password=$(cat '{self._token_file}')\"; }}; f"
            )
        self._c_flags = [item for flag in flags for item in ("-c", flag)]

    def _check_repository(self) -> None:
        # GIT_DIR/GIT_WORK_TREE are forced to GIT_REPO_ROOT on every call (I4),
        # so `rev-parse --show-toplevel` would be true by construction and prove
        # nothing. What can actually be wrong is that .git isn't a usable
        # repository, which a plain rev-parse reveals.
        rc, _, _ = self._git(["rev-parse", "--git-dir"], rc_ok=(0, 128))
        if rc != 0:
            raise RuntimeError(
                f"{self.git_dir} is not a usable git repository. Mount the repository ROOT at "
                "GIT_REPO_ROOT."
            )
        if self._rev("HEAD") is None:
            raise RuntimeError(
                "the repository has no commits yet. Make an initial commit on the host first; "
                "the git tools need a HEAD to diff against."
            )
        rc, out, _ = self._git(["symbolic-ref", "-q", "HEAD"], rc_ok=(0, 1))
        if rc != 0:
            raise RuntimeError("HEAD is detached; check out a branch before starting git mode.")
        current = out.decode().strip().removeprefix("refs/heads/")
        if not self.branch:
            self.branch = current
        elif self.branch != current:
            raise RuntimeError(
                f"the checked-out branch ({current}) does not match GIT_BRANCH ({self.branch})"
            )
        if self.write_enabled:
            if not _REMOTE_NAME_RE.match(self.remote):
                raise RuntimeError(f"GIT_REMOTE is not a plain remote name: {self.remote!r}")
            rc, _, _ = self._git(
                ["check-ref-format", "--branch", self.branch], rc_ok=(0, 1, 128)
            )
            if rc != 0 or self.branch.startswith("-"):
                raise RuntimeError(f"GIT_BRANCH is not a valid branch name: {self.branch!r}")
            remotes = self._out(["remote"]).split()
            if self.remote not in remotes:
                raise RuntimeError(
                    f"remote '{self.remote}' is not configured in {self.repo_root} (GIT_REMOTE)"
                )
            for flag in ([], ["--push"]):
                url = self._out(["remote", "get-url", *flag, "--", self.remote]).strip()
                if not self._url_allowed(url):
                    raise RuntimeError(
                        f"remote '{self.remote}' has a URL scheme git mode refuses (only ssh://, "
                        "scp-style user@host:path, and https:// are allowed; file://, local paths "
                        "and ext:: never are)."
                    )

    @staticmethod
    def _url_allowed(url: str) -> bool:
        return any(re.match(p, url) for p in (*_URL_PATTERNS, *EXTRA_URL_PATTERNS))

    def _check_config_origins(self) -> None:
        """I3: refuse if git would read a config file the agent could have
        written. The local config lives in .git (which no file tool can touch),
        but it can `[include]` a file elsewhere in the working tree."""
        raw = self._git(["config", "--list", "--show-origin", "-z", "--includes"])[1]
        origins: set[str] = set()
        for entry in raw.split(b"\0"):
            if entry.startswith(b"file:"):
                origins.add(entry[5:].split(b"\n", 1)[0].decode(errors="replace"))
        git_dir = self.git_dir + os.sep
        for origin in origins:
            path = os.path.realpath(os.path.join(self.repo_root, origin))
            inside_tree = path == self.repo_root or path.startswith(self.repo_root + os.sep)
            inside_git = path.startswith(git_dir)
            if inside_tree and not inside_git:
                raise RuntimeError(
                    f"git config includes {path}, which is inside the working tree but outside "
                    ".git. An agent that can write that file could make git run programs. "
                    "Remove the [include] from .git/config or move the file out of the tree."
                )

    def _check_write_boundary(self) -> None:
        if not self.scope:
            self.write_boundary = "n/a"
            if self.pull_outside_scope:
                log.info(
                    "GIT_PULL_OUTSIDE_SCOPE has no effect: the shared folder is the whole repository"
                )
            return
        readonly = bool(os.statvfs(self.repo_root).f_flag & os.ST_RDONLY)
        self.write_boundary = "kernel" if readonly else "software"
        if not readonly:
            log.warning(
                "GIT_REPO_ROOT (%s) is mounted read-write. Writes outside the shared folder are "
                "refused by this server's scope checks, but not by the kernel. Mount the repo "
                "root read-only, with only .git and the shared folder writable, to enforce the "
                "boundary in the kernel too (docs/plan-mcp-git-ops.md, 'Mount layout').",
                self.repo_root,
            )
        if self.pull_outside_scope and readonly:
            raise RuntimeError(
                "GIT_PULL_OUTSIDE_SCOPE=true needs GIT_REPO_ROOT mounted read-write: a pull that "
                "updates files outside the shared folder cannot go through a read-only mount. "
                "Either mount the repo root read-write (the write boundary is then enforced by "
                "this server only) or unset GIT_PULL_OUTSIDE_SCOPE."
            )

    def _check_write_mode(self) -> None:
        for what, path in (("the .git directory", self.git_dir), ("DATA_ROOT", self._data_abs)):
            ro = bool(os.statvfs(path).f_flag & os.ST_RDONLY)
            if ro or not os.access(path, os.W_OK):
                raise RuntimeError(
                    f"{what} ({path}) is not writable but ENABLE_GIT_WRITE is on. In the "
                    "read-only-root layout, .git and the shared folder must be their own "
                    "read-write mounts."
                )

    # ======================================================================
    # Running git
    # ======================================================================

    def _env(self, write: bool) -> dict[str, str]:
        """Built from scratch. The container's own environment (API_TOKEN,
        GIT_TOKEN, ...) never reaches git or ssh except through these names."""
        env = {
            "PATH": "/usr/bin:/bin",
            "HOME": self._state_dir,
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "GIT_DIR": self.git_dir,
            "GIT_WORK_TREE": self.repo_root,
            "GIT_CEILING_DIRECTORIES": "/",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": self._global_config,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "/bin/false",
            "SSH_ASKPASS": "/bin/false",
            "GIT_EDITOR": "/bin/true",
            "GIT_PAGER": "cat",
            "GIT_SSH_COMMAND": self._ssh_command,
        }
        if write:
            env.update(
                GIT_AUTHOR_NAME=self.cfg.git_author_name,
                GIT_AUTHOR_EMAIL=self.cfg.git_author_email,
                GIT_COMMITTER_NAME=self.cfg.git_author_name,
                GIT_COMMITTER_EMAIL=self.cfg.git_author_email,
            )
        else:
            env["GIT_OPTIONAL_LOCKS"] = "0"
        return env

    def _run(
        self, argv: list[str], env: dict[str, str], stdin: bytes | None, timeout: float
    ) -> tuple[int, bytes, bytes]:
        try:
            proc = subprocess.Popen(
                argv,
                env=env,
                cwd=self.repo_root,
                stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,  # own process group, so a timeout can kill ssh too
            )
        except OSError as exc:
            raise GitOpError("git_failed", f"could not run git: {exc.strerror}") from exc

        def kill_group() -> None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGKILL)

        # The pipes are drained in threads so that (a) a large stdout can't
        # deadlock against a large stdin, and (b) the output cap is a REAL cap:
        # communicate() would read everything into memory and only then
        # truncate, which protects nothing against a pathological diff.
        over = threading.Event()
        out_chunks: list[bytes] = []
        err_chunks: list[bytes] = []

        def drain(stream: Any, chunks: list[bytes], limit: int, fatal: bool) -> None:
            total = 0
            for chunk in iter(lambda: stream.read(65536), b""):
                total += len(chunk)
                if total <= limit:
                    chunks.append(chunk)
                elif fatal and not over.is_set():
                    over.set()
                    kill_group()
                # Past the limit a non-fatal stream (stderr) just discards.

        readers = [
            threading.Thread(target=drain, args=(proc.stdout, out_chunks, MAX_OUTPUT_BYTES, True), daemon=True),
            threading.Thread(target=drain, args=(proc.stderr, err_chunks, 1024 * 1024, False), daemon=True),
        ]
        for t in readers:
            t.start()
        if stdin is not None:
            with contextlib.suppress(BrokenPipeError, OSError):
                proc.stdin.write(stdin)  # type: ignore[union-attr]
            with contextlib.suppress(BrokenPipeError, OSError):
                proc.stdin.close()  # type: ignore[union-attr]
        deadline = time.monotonic() + timeout
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            kill_group()
            with contextlib.suppress(Exception):
                proc.wait(timeout=5)
            raise GitOpError(
                "timeout",
                f"git did not finish within {int(timeout)}s and was killed. Nothing was left "
                "locked (network and working-tree steps never share a process).",
            ) from None
        for t in readers:
            t.join(timeout=max(0.5, deadline - time.monotonic()))
        if any(t.is_alive() for t in readers):
            kill_group()  # a grandchild (ssh) is still holding a pipe open
            for t in readers:
                t.join(timeout=5)
        if over.is_set():
            raise GitOpError(
                "git_failed",
                f"git produced more than {MAX_OUTPUT_BYTES // (1024 * 1024)} MiB of output and was "
                "stopped. Narrow the request (a path, or a smaller limit).",
            )
        return proc.returncode, b"".join(out_chunks), b"".join(err_chunks)

    def _git(
        self,
        args: list[str],
        *,
        write: bool = False,
        stdin: bytes | None = None,
        env_extra: dict[str, str] | None = None,
        timeout: float | None = None,
        rc_ok: tuple[int, ...] = (0,),
        network: bool = False,
    ) -> tuple[int, bytes, str]:
        argv = [self.git_bin, *self._c_flags, *args]
        env = self._env(write)
        if env_extra:
            env.update(env_extra)
        if timeout is None:
            timeout = (
                self.cfg.git_network_timeout_seconds if network else self.cfg.git_local_timeout_seconds
            )
        attempts = 2 if write else 1
        for attempt in range(attempts):
            rc, out, err = self._run(argv, env, stdin, timeout)
            if rc in rc_ok:
                return rc, out, err.decode(errors="replace")
            error = self._error(rc, out, err)
            if error.code == "locked" and attempt + 1 < attempts:
                time.sleep(2.0)  # a host-side git may be mid-operation; the lock is never deleted
                continue
            raise error
        raise AssertionError("unreachable")

    def _out(self, args: list[str], **kw: Any) -> str:
        return self._git(args, **kw)[1].decode(errors="replace")

    def _rev(self, rev: str) -> str | None:
        rc, out, _ = self._git(
            ["rev-parse", "--verify", "--quiet", "--end-of-options", rev], rc_ok=(0, 1)
        )
        return out.decode().strip() if rc == 0 else None

    # -- errors ------------------------------------------------------------

    def _scrub(self, text: str) -> str:
        text = re.sub(r"((?:https?|ssh)://)[^/\s@]+@", r"\1", text)
        if self.cfg.git_token:
            text = text.replace(self.cfg.git_token, "<redacted>")
        lines = [
            ln for ln in text.splitlines()
            if "claude-files-git" not in ln and (not self._state_dir or self._state_dir not in ln)
        ]
        return "\n".join(lines[-20:])[-4096:].strip()

    def _error(self, rc: int, out: bytes, err: bytes) -> GitOpError:
        text = (err + b"\n" + out).decode(errors="replace")
        low = text.lower()
        tail = self._scrub(text)

        def has(*needles: str) -> bool:
            return any(n in low for n in needles)

        if has("index.lock", "another git process seems to be running") or (
            "unable to create" in low and ".lock" in low
        ):
            return GitOpError(
                "locked",
                "another git process holds the repository lock (a person running git on the host?). "
                "Retry in a moment; the lock was not touched.",
            )
        if has(
            "permission denied (publickey", "authentication failed", "could not read username",
            "could not read password", "invalid username or password", "terminal prompts disabled",
            "returned error: 401", "returned error: 403", "access denied", "repository not found",
        ):
            return GitOpError(
                "auth_failed", f"the remote refused the credential (deploy key or token):\n{tail}"
            )
        if has(
            "could not resolve host", "connection refused", "connection timed out",
            "network is unreachable", "no route to host", "host key verification failed",
            "connection reset", "unable to access", "could not read from remote repository",
            "ssh: connect to host", "operation timed out", "kex_exchange_identification",
        ):
            return GitOpError("network", f"could not reach the remote:\n{tail}")
        if has(
            "non-fast-forward", "fetch first", "[rejected]", "updates were rejected",
            "behind its remote counterpart", "tip of your current branch is behind",
        ):
            return GitOpError(
                "non_fast_forward",
                "the remote has commits you don't have. Run git_pull, then git_push again.",
            )
        if has(
            "would be overwritten by merge", "would be overwritten by checkout",
            "cannot rebase: you have unstaged changes",
            "cannot rebase: your index contains uncommitted changes",
            "your local changes to the following files", "not uptodate. cannot merge",
            "please commit your changes or stash them",
            "untracked working tree files would be overwritten",
        ):
            return self._dirty_error(tail)
        if has("conflict (", "automatic merge failed", "could not apply"):
            return GitOpError("conflict", f"the operation conflicted:\n{tail}")
        return GitOpError("git_failed", f"git failed (exit {rc}):\n{tail}" if tail else f"git failed (exit {rc})")

    def _dirty_error(self, tail: str) -> GitOpError:
        """Name the blocking files, split by who can fix them. A merge refuses
        on *staged* changes anywhere and a rebase on *any* dirty tracked file,
        including ones outside the scope that only a person on the host can
        deal with -- so the message has to say which is which."""
        mine: list[str] = []
        theirs: list[str] = []
        with contextlib.suppress(Exception):
            for ch in self._status_entries():
                if ch["index"] in ("?", "!") or ch.get("untracked"):
                    continue
                if ch["index"] == " " and ch["worktree"] == " ":
                    continue
                (mine if ch["in_scope"] else theirs).append(ch["scope_path"] or ch["path"])
        parts = ["uncommitted changes block this operation."]
        if mine:
            parts.append(f"In your folder (git_commit them first): {_listing(mine)}.")
        if theirs:
            parts.append(
                f"Outside your folder, which only a person on the host can commit or unstage: "
                f"{_listing(theirs)}."
            )
        if not mine and not theirs:
            parts.append(tail)
        return GitOpError("dirty_worktree", " ".join(parts), {"in_scope": mine, "outside_scope": theirs})

    # ======================================================================
    # Scope engine
    # ======================================================================

    def in_scope(self, path: str) -> bool:
        return not self.scope or path.startswith(self.scope)

    def scope_path(self, path: str) -> str | None:
        if not self.scope:
            return path
        return path[len(self.scope):] if path.startswith(self.scope) else None

    def _split_names(self, raw: bytes) -> list[str]:
        return [n.decode("utf-8", "surrogateescape") for n in raw.split(b"\0") if n]

    def _names(self, a: str, b: str) -> list[str]:
        """Paths that differ between two tree-ishes. --no-renames, so both
        sides of a rename appear: a file moved from outside the scope into it
        touches the outside."""
        raw = self._git(["diff-tree", "-r", "-z", "--name-only", "--no-renames", a, b])[1]
        return self._split_names(raw)

    def _outside(self, paths: list[str]) -> list[str]:
        return [p for p in paths if not self.in_scope(p)]

    def _is_ancestor(self, a: str, b: str) -> bool:
        rc, _, _ = self._git(["merge-base", "--is-ancestor", a, b], rc_ok=(0, 1))
        return rc == 0

    def _pathspecs(self, rels: list[str] | None = None) -> list[str]:
        """Positive pathspecs for the scope (or for validated scope-relative
        paths under it), plus the .upload-*.tmp exclusions autocommit.sh uses.
        ':(literal)' means '*' and ':(...)' magic in an agent-supplied path
        cannot widen it."""
        if rels:
            specs = [":(literal)" + self.scope + r for r in rels]
        elif self.scope:
            specs = [":(literal)" + self.scope_dir]
        else:
            specs = ["."]
        prefix = _escape_glob(self.scope)
        specs += [
            f":(exclude,glob){prefix}**/.upload-*.tmp",
            f":(exclude,glob){prefix}.upload-*.tmp",
        ]
        return specs

    # ======================================================================
    # Summaries
    # ======================================================================

    def _remote_ref(self) -> str:
        return f"refs/remotes/{self.remote}/{self.branch}"

    def _operation_in_progress(self) -> str | None:
        for name, label in (
            ("MERGE_HEAD", "merge"), ("rebase-merge", "rebase"), ("rebase-apply", "rebase"),
            ("CHERRY_PICK_HEAD", "cherry-pick"), ("REVERT_HEAD", "revert"), ("BISECT_LOG", "bisect"),
        ):
            if os.path.exists(os.path.join(self.git_dir, name)):
                return label
        return None

    def branch_summary(self) -> dict[str, Any]:
        head = self._rev("HEAD")
        rc, out, _ = self._git(["symbolic-ref", "-q", "--short", "HEAD"], rc_ok=(0, 1))
        name = out.decode().strip() if rc == 0 else None
        upstream = None
        ahead = behind = None
        if self.remote:
            ref = self._remote_ref()
            if self._rev(ref):
                upstream = f"{self.remote}/{self.branch}"
                rc, out, _ = self._git(
                    ["rev-list", "--left-right", "--count", f"HEAD...{ref}"], rc_ok=(0, 1)
                )
                if rc == 0:
                    a, b = out.decode().split()
                    ahead, behind = int(a), int(b)
        fetched = None
        with contextlib.suppress(OSError):
            m = os.stat(os.path.join(self.git_dir, "FETCH_HEAD")).st_mtime
            fetched = datetime.datetime.fromtimestamp(m, datetime.timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
        return {
            "name": name,
            "head": _short(head),
            "upstream": upstream,
            "ahead": ahead,
            "behind": behind,
            "as_of_fetch": fetched,
            "operation_in_progress": self._operation_in_progress(),
        }

    def info_block(self) -> dict[str, Any]:
        scope_label = self.scope or "."
        if self.scope:
            where = (
                f"Your folder is {self.scope} inside a larger repository. git_status shows "
                "changes across the whole repository; you can only commit, pull, merge or push "
                "changes inside your folder, and those tools refuse (outside_scope / "
                "outside_scope_commits) rather than touch anything else."
            )
        else:
            where = "Your folder is the whole repository."
        tools = []
        if self.read_enabled:
            tools.append("Read tools: git_status, git_log, git_diff (git_diff covers only your folder).")
        if self.write_enabled:
            tools.append(
                "Write tools (rw token): git_commit, git_pull, git_push, git_merge, git_fetch. "
                "Push is never forced; on non_fast_forward, git_pull then git_push. Merge "
                "conflicts are detected before anything changes."
            )
        return {
            "read_enabled": self.read_enabled,
            "write_enabled": self.write_enabled,
            "scope": scope_label,
            "write_boundary": self.write_boundary,
            "pull_outside_scope": self.pull_outside_scope,
            "remote": self.remote if self.write_enabled else None,
            "branch": self.branch,
            "hooks": "disabled",
            "max_changed_files": self.cfg.git_max_changed_files,
            "note": " ".join(
                [where, *tools,
                 "ahead/behind are only as fresh as the last fetch (see as_of_fetch). Nothing "
                 "is committed or pushed automatically: call git_commit and git_push yourself."]
            ),
        }

    @staticmethod
    def disabled_info() -> dict[str, Any]:
        return {
            "read_enabled": False,
            "write_enabled": False,
            "note": "Git tools are disabled on this server (ENABLE_GIT_READ / ENABLE_GIT_WRITE).",
        }

    # ======================================================================
    # Guards shared by the write tools
    # ======================================================================

    def _need_read(self) -> None:
        if not self.read_enabled:
            raise GitOpError("git_disabled", "git read tools are disabled on this server (ENABLE_GIT_READ=false)")

    def _need_write(self) -> None:
        if not self.write_enabled:
            raise GitOpError("git_disabled", "git write tools are disabled on this server (ENABLE_GIT_WRITE=false)")

    @contextmanager
    def _exclusive(self) -> Iterator[None]:
        try:
            with self.gate.exclusive():
                yield
        except GateTimeout as exc:
            raise GitOpError("git_busy", str(exc)) from exc

    def _require_branch(self) -> None:
        """Re-checked on every write: the host can `git checkout` after startup."""
        rc, out, _ = self._git(["symbolic-ref", "-q", "HEAD"], rc_ok=(0, 1))
        if rc != 0:
            raise GitOpError("detached_head", "HEAD is detached; ask the user to check out a branch on the host.")
        current = out.decode().strip().removeprefix("refs/heads/")
        if current != self.branch:
            raise GitOpError(
                "wrong_branch",
                f"the checked-out branch is '{current}' but this server only works on "
                f"'{self.branch}'. Ask the user to check '{self.branch}' out on the host.",
            )
        op = self._operation_in_progress()
        if op:
            raise GitOpError(
                "operation_in_progress",
                f"a {op} is in progress in the repository (started on the host); finish or abort "
                "it there first.",
            )

    def _check_uploads(self) -> None:
        busy = self._uploads_in_flight()
        if busy:
            raise GitOpError(
                "uploads_in_flight",
                "a pull or merge could delete or rename a folder an upload is writing into. "
                f"Finish (commit_write / abort_write) or wait out these first: {_listing(busy)}.",
                {"paths": busy},
            )

    # ======================================================================
    # Read tools
    # ======================================================================

    def _status_entries(self) -> list[dict[str, Any]]:
        raw = self._git(["status", "--porcelain=v2", "--branch", "-z", "--untracked-files=all"])[1]
        fields = raw.split(b"\0")
        entries: list[dict[str, Any]] = []
        i = 0

        def entry(path: str, index: str, worktree: str, **extra: Any) -> dict[str, Any]:
            e = {
                "path": path,
                "scope_path": self.scope_path(path),
                "in_scope": self.in_scope(path),
                "index": index,
                "worktree": worktree,
            }
            e.update(extra)
            return e

        while i < len(fields):
            f = fields[i].decode("utf-8", "surrogateescape")
            i += 1
            if not f or f.startswith("#"):
                continue
            kind = f[0]
            if kind == "1":
                parts = f.split(" ", 8)
                xy, path = parts[1], parts[8]
                entries.append(entry(path, xy[0].replace(".", " "), xy[1].replace(".", " ")))
            elif kind == "2":
                parts = f.split(" ", 9)
                xy, path = parts[1], parts[9]
                orig = fields[i].decode("utf-8", "surrogateescape")
                i += 1
                entries.append(
                    entry(
                        path, xy[0].replace(".", " "), xy[1].replace(".", " "),
                        **{"from": orig, "from_scope_path": self.scope_path(orig)},
                    )
                )
            elif kind == "u":
                parts = f.split(" ", 10)
                xy, path = parts[1], parts[10]
                entries.append(entry(path, xy[0], xy[1], conflicted=True))
            elif kind == "?":
                entries.append(entry(f[2:], "?", "?", untracked=True))
            # "!" (ignored) lines are not requested and are skipped.
        return [e for e in entries if not _UPLOAD_TMP_RE.search(e["path"])]

    def status_tool(self, *, fetch: bool, token_rw: bool, limit: int = 500) -> dict[str, Any]:
        """git_status as the front ends call it. `fetch=True` contacts the
        remote and moves refs/remotes/*, so it is a *write*: it needs the write
        switch AND a read-write token, however read-only the rest of status is."""
        self._need_read()
        if fetch:
            if not self.write_enabled:
                raise GitOpError(
                    "git_disabled",
                    "git_status(fetch=true) contacts the remote, which needs the git write tools "
                    "(ENABLE_GIT_WRITE=false on this server). Call git_status without fetch.",
                )
            if not token_rw:
                raise GitOpError(
                    "readonly",
                    "git_status(fetch=true) contacts the remote and needs a read-write token. "
                    "Call it without fetch, or ask the user for a read-write token.",
                )
            self.fetch()
        return self.status(limit=limit)

    def status(self, *, limit: int = 500) -> dict[str, Any]:
        self._need_read()
        entries = self._status_entries()
        in_scope = [e for e in entries if e["in_scope"]]
        outside = [e for e in entries if not e["in_scope"]]
        limit = max(1, min(int(limit), 5000))
        # In-scope entries first, so truncation drops outside-scope noise
        # before the agent's own files.
        ordered = in_scope + outside
        return {
            "branch": self.branch_summary(),
            "scope": self.scope or ".",
            "changes": ordered[:limit],
            "summary": {
                "in_scope": len(in_scope),
                "outside_scope": len(outside),
                "conflicted": sum(1 for e in entries if e.get("conflicted")),
            },
            "truncated": len(ordered) > limit,
            "clean": not entries,
        }

    def log(self, *, limit: int = 20, scope_only: bool = False) -> dict[str, Any]:
        self._need_read()
        limit = max(1, min(int(limit), 200))
        fmt = "%x1e%H%x1f%an%x1f%aI%x1f%s"
        args = ["log", f"-n{limit}", f"--format={fmt}", "--no-color"]
        if scope_only and self.scope:
            args += ["--", ":(literal)" + self.scope_dir]
        raw = self._out(args)
        commits: list[dict[str, Any]] = []
        for rec in raw.split("\x1e"):
            rec = rec.strip("\n")
            if not rec:
                continue
            sha, author, date, subject = (rec.split("\x1f") + ["", "", "", ""])[:4]
            outside = False
            if self.scope:
                outside = bool(self._outside(self._commit_names(sha)[0]))
            commits.append(
                {
                    "hash": sha, "short": sha[:7], "author": author, "date": date,
                    "subject": subject, "touches_outside_scope": outside,
                }
            )
        return {"commits": commits, "branch": self.branch_summary()}

    def _commit_names(self, sha: str) -> tuple[list[str], list[list[str]]]:
        """(names against the first parent, names against each parent)."""
        parents = self._out(["rev-list", "--parents", "-n1", sha]).split()[1:]
        if not parents:
            raw = self._git(["diff-tree", "-r", "-z", "--name-only", "--no-renames",
                             "--no-commit-id", "--root", sha])[1]
            names = self._split_names(raw)
            return names, [names]
        per_parent = [self._names(p, sha) for p in parents]
        return per_parent[0], per_parent

    def diff(
        self,
        *,
        staged: bool = False,
        path: str | None = None,
        offset: int = 0,
        length: int | None = None,
        inline_limit: int = 100_000,
    ) -> dict[str, Any]:
        """The diff of the *scope* only, always (plan, Decisions 2): the
        pathspec is the scope or a validated path under it, so a change outside
        the folder never appears here, staged or not."""
        self._need_read()
        rel = self._scope_rel(path) if path else ""
        rels = [rel] if rel else None
        args = ["diff", "--no-ext-diff", "--no-textconv", "--no-color"]
        if staged:
            args.append("--cached")
        args += ["--", *self._pathspecs(rels)]
        text = self._out(args)
        total = len(text)
        offset = max(0, int(offset))
        n = inline_limit if length is None else max(0, min(int(length), inline_limit))
        chunk = text[offset:offset + n]
        truncated = offset + len(chunk) < total
        result: dict[str, Any] = {
            "diff": chunk,
            "offset": offset,
            "total_chars": total,
            "truncated": truncated,
            "scope": self.scope or ".",
        }
        if truncated:
            result["note"] = (
                f"Showing characters {offset}-{offset + len(chunk)} of {total}. Call again with "
                f"offset={offset + len(chunk)} for the rest, or pass path to narrow it."
            )
        if not text:
            result["note"] = (
                "No changes to tracked files in your folder"
                + (" (staged)" if staged else "")
                + ". git_diff does not show untracked files; use git_status for those."
            )
        return result

    def _scope_rel(self, raw: str) -> str:
        """A scope-relative path from the agent, validated exactly like a file
        tool's path (so '..', absolute paths and denied names fail the same
        way), as the repo-relative-to-scope string used in pathspecs."""
        if not isinstance(raw, str):
            raise GitOpError("invalid_argument", "path must be a string")
        parts = self.vault.components(raw)
        return "/".join(parts)

    # ======================================================================
    # Write tools
    # ======================================================================

    def _validate_message(self, message: str) -> str:
        if not isinstance(message, str):
            raise GitOpError("invalid_argument", "message must be a string")
        msg = message.strip()
        if not msg:
            raise GitOpError("invalid_argument", "message must not be empty")
        if "\0" in msg:
            raise GitOpError("invalid_argument", "message must not contain NUL bytes")
        if len(msg) > MAX_MESSAGE_CHARS:
            raise GitOpError("invalid_argument", f"message is over {MAX_MESSAGE_CHARS} characters")
        if self.cfg.git_commit_trailer:
            msg += "\n\n" + self.cfg.git_commit_trailer
        return msg + "\n"

    def commit(self, message: str, paths: list[str] | None = None) -> dict[str, Any]:
        """Commit changes inside the scope only.

        Built with plumbing against a *temporary index*, so anything a person
        staged on the host outside the scope cannot ride along in a plain
        `git commit` (docs/plan-mcp-git-ops.md, 'git_commit')."""
        self._need_write()
        msg = self._validate_message(message)
        rels: list[str] | None = None
        if paths is not None:
            if not isinstance(paths, list) or not paths:
                raise GitOpError(
                    "invalid_argument",
                    "paths must be a non-empty list of strings; omit it to commit everything in "
                    "your folder",
                )
            rels = [self._scope_rel(p) for p in paths]
            rels = [r for r in rels if r] or None   # "" means the scope root

        specs = self._pathspecs(rels)
        with self._lock, self._exclusive():
            self._require_branch()
            old = self._rev("HEAD")
            assert old is not None
            tmp_index = os.path.join(self._state_dir, f"index.{os.urandom(6).hex()}")
            idx = {"GIT_INDEX_FILE": tmp_index}
            try:
                self._git(["read-tree", old], write=True, env_extra=idx)
                self._git(
                    ["add", "-A", "--", *specs], write=True, env_extra=idx,
                    timeout=max(self.cfg.git_local_timeout_seconds, 120),
                )
                limit = self.cfg.git_max_changed_files
                staged = self._split_names(
                    self._git(["diff-index", "--cached", "--name-only", "-z", old],
                              write=True, env_extra=idx)[1]
                )
                if limit > 0 and len(staged) > limit:
                    raise GitOpError(
                        "too_many_changes",
                        f"{len(staged)} files changed, over GIT_MAX_CHANGED_FILES={limit}. This "
                        "guard stops a runaway agent from committing a mass change or deletion. "
                        "Nothing was committed; commit a narrower set with paths=[...].",
                    )
                tree = self._git(["write-tree"], write=True, env_extra=idx)[1].decode().strip()
            finally:
                with contextlib.suppress(OSError):
                    os.unlink(tmp_index)

            old_tree = self._out(["rev-parse", f"{old}^{{tree}}"]).strip()
            if tree == old_tree:
                outside = [e for e in self._status_entries() if not e["in_scope"]]
                extra = (
                    f" There are {len(outside)} changed file(s) outside your folder; those are not "
                    "yours to commit (see git_status)."
                    if outside else ""
                )
                raise GitOpError("nothing_to_commit", "no changes to commit inside your folder." + extra)

            # I8: verify the tree before any ref moves. This cannot fail given
            # the pathspecs above; it exists so a future bug in them fails
            # closed instead of open.
            leaked = self._outside(self._names(old, tree))
            if leaked:
                raise GitOpError(
                    "outside_scope",
                    f"refusing to commit: the commit would change files outside your folder "
                    f"({_listing(leaked)}). Nothing was committed.",
                )

            new = self._git(
                ["commit-tree", tree, "-p", old, "-F", "-"], write=True, stdin=msg.encode()
            )[1].decode().strip()
            # Compare-and-swap: if someone committed on the host meanwhile, this
            # fails ("locked") instead of silently dropping their commit.
            try:
                self._git(
                    ["update-ref", "-m", "claude-files: commit",
                     f"refs/heads/{self.branch}", new, old],
                    write=True,
                )
            except GitOpError as exc:
                if exc.code in ("git_failed", "locked"):
                    raise GitOpError(
                        "locked",
                        "the branch moved while committing (someone committed on the host). "
                        "Nothing was lost; call git_status and retry.",
                    ) from exc
                raise

            # Bring the real index's entries for the committed paths up to date,
            # so git_status shows them clean. Entries outside the scope are left
            # exactly as the person on the host left them.
            note = None
            try:
                self._git(["add", "-A", "--", *specs], write=True)
            except GitOpError:
                note = (
                    "committed, but the index could not be refreshed (another git process holds "
                    "the lock); git_status may show these files as staged until it is."
                )

            files = ins = dels = 0
            for line in self._out(["diff-tree", "-r", "--numstat", "--no-renames", old, new]).splitlines():
                cols = line.split("\t", 2)
                if len(cols) == 3:
                    files += 1
                    ins += int(cols[0]) if cols[0].isdigit() else 0
                    dels += int(cols[1]) if cols[1].isdigit() else 0
            left = 0
            with contextlib.suppress(Exception):
                left = sum(1 for e in self._status_entries() if not e["in_scope"])
            result: dict[str, Any] = {
                "ok": True,
                "commit": new,
                "files_changed": files,
                "insertions": ins,
                "deletions": dels,
                "outside_scope_changes_left": left,
                "branch": self.branch_summary(),
            }
            if note:
                result["note"] = note
            return result

    # -- network -----------------------------------------------------------

    def _fetch_locked(self) -> dict[str, Any]:
        ref = self._remote_ref()
        before = self._rev(ref)
        self._git(
            ["fetch", "--no-tags", self.remote, f"+refs/heads/{self.branch}:{ref}"],
            write=True, network=True,
        )
        after = self._rev(ref)
        updated = (
            [{"ref": f"{self.remote}/{self.branch}", "old": _short(before), "new": _short(after)}]
            if before != after else []
        )
        return {"updated_refs": updated}

    def fetch(self) -> dict[str, Any]:
        self._need_write()
        with self._lock:
            self._require_branch()
            res = self._fetch_locked()
            return {"ok": True, **res, "branch": self.branch_summary()}

    def push(self) -> dict[str, Any]:
        self._need_write()
        with self._lock:
            self._require_branch()
            ref = self._remote_ref()
            remote_sha = self._rev(ref)
            if remote_sha is None:
                raise GitOpError(
                    "stale_remote",
                    f"no remote-tracking ref for {self.remote}/{self.branch} yet; run git_fetch "
                    "first so the scope check knows what is already published.",
                )
            head = self._rev("HEAD")
            if head == remote_sha or (head and self._is_ancestor(head, remote_sha)):
                return {"ok": True, "pushed": None, "commits": 0, "branch": self.branch_summary()}
            self._check_push_range(ref)
            _, out, _ = self._git(
                ["push", "--porcelain", self.remote, f"HEAD:refs/heads/{self.branch}"],
                write=True, network=True,
            )
            commits = int(self._out(["rev-list", "--count", f"{remote_sha}..{head}"]).strip() or 0) \
                if self._is_ancestor(remote_sha, head) else None
            new_remote = self._rev(ref) or head
            return {
                "ok": True,
                "pushed": f"{_short(remote_sha)}..{_short(new_remote)}",
                "commits": commits,
                "branch": self.branch_summary(),
            }

    def _check_push_range(self, remote_ref: str) -> None:
        """I8 for push: every commit about to be published must change only
        scope paths. Checked per commit, not on the net diff, so a host commit
        to sub-folder-2 followed by a revert (empty net diff) is still refused:
        the agent must never publish a host-authored out-of-scope change."""
        if not self.scope:
            return
        lines = self._out(["rev-list", "--parents", f"{remote_ref}..HEAD"]).splitlines()
        if len(lines) > MAX_PUSH_RANGE:
            raise GitOpError(
                "outside_scope_commits",
                f"{len(lines)} unpushed commits is more than the {MAX_PUSH_RANGE} this server "
                "will scope-check. Push from the host.",
            )
        bad: list[dict[str, Any]] = []
        for line in lines:
            sha = line.split()[0]
            _, per_parent = self._commit_names(sha)
            # A non-merge commit has one parent; a merge is fine if it is
            # scope-only relative to at least one parent (the other parent's
            # out-of-scope content is already on the remote by induction).
            if not any(not self._outside(names) for names in per_parent):
                outside = self._outside(per_parent[0])
                subject = self._out(["log", "-1", "--format=%s", sha]).strip()
                bad.append({"commit": sha[:7], "subject": subject, "paths": outside[:10]})
        if bad:
            lst = "; ".join(f"{b['commit']} \"{b['subject']}\" touches {_listing(b['paths'], 3)}" for b in bad[:5])
            raise GitOpError(
                "outside_scope_commits",
                "refusing to push: these unpushed commits change files outside your folder, which "
                f"were committed outside claude-files and must be pushed from the host: {lst}.",
                {"commits": bad},
            )

    # -- integrate (pull / merge) ----------------------------------------

    def pull(self, strategy: str = "ff-only") -> dict[str, Any]:
        self._need_write()
        if strategy not in ("ff-only", "merge", "rebase"):
            raise GitOpError("invalid_argument", "strategy must be 'ff-only', 'merge' or 'rebase'")
        with self._lock:
            self._require_branch()
            self._check_uploads()
            self._fetch_locked()
            ref = self._remote_ref()
            target = self._rev(ref)
            if target is None:
                raise GitOpError("stale_remote", f"{self.remote}/{self.branch} does not exist on the remote")
            return self._integrate(
                target, strategy=strategy, from_remote=True,
                label=f"{self.remote}/{self.branch}", no_ff=False,
            )

    def merge(self, ref: str, no_ff: bool = False) -> dict[str, Any]:
        self._need_write()
        with self._lock:
            self._require_branch()
            full, sha, from_remote = self._resolve_merge_ref(ref)
            self._check_uploads()
            return self._integrate(sha, strategy="merge", from_remote=from_remote, label=ref, no_ff=bool(no_ff))

    def _resolve_merge_ref(self, ref: str) -> tuple[str, str, bool]:
        """I6: the only ref an agent ever supplies. Validated, resolved to a full
        refname under refs/heads/ or refs/remotes/<GIT_REMOTE>/, and only the
        resolved *sha* ever reaches git's argv."""
        def bad(why: str) -> GitOpError:
            return GitOpError(
                "invalid_ref",
                f"{why}. Pass a local branch name or '{self.remote}/<branch>'; raw SHAs, tags and "
                "revision syntax are not accepted.",
            )

        if not isinstance(ref, str) or not (1 <= len(ref) <= 255):
            raise bad("ref must be 1-255 characters")
        if ref.startswith("-") or not _MERGE_REF_RE.match(ref):
            raise bad("ref contains characters a branch name cannot")
        from_remote = ref.startswith(self.remote + "/")
        full = f"refs/remotes/{ref}" if from_remote else f"refs/heads/{ref}"
        rc, _, _ = self._git(["check-ref-format", full], rc_ok=(0, 1, 128))
        if rc != 0:
            raise bad("not a valid branch name")
        if full == f"refs/heads/{self.branch}":
            raise bad("cannot merge the current branch into itself")
        sha = self._rev(f"{full}^{{commit}}")
        if sha is None:
            raise bad(f"no such branch: {ref}")
        return full, sha, from_remote

    def _merge_tree(self, ours: str, theirs: str) -> str:
        """Compute a merge's result tree WITHOUT touching the index or working
        tree. Raises `conflict` (nothing changed) if it would conflict."""
        rc, out, err = self._git(
            ["merge-tree", "--write-tree", "-z", "--name-only", ours, theirs],
            write=True, rc_ok=(0, 1),
        )
        fields = out.split(b"\0")
        tree = fields[0].decode().strip()
        if rc == 1:
            names = []
            for f in fields[1:]:
                if not f:
                    break
                names.append(f.decode("utf-8", "surrogateescape"))
            mine = [self.scope_path(n) or n for n in names if self.in_scope(n)]
            theirs_ = [n for n in names if not self.in_scope(n)]
            where = f" ({_listing(mine)})" if mine else ""
            if theirs_:
                where += f" and outside your folder ({_listing(theirs_)})"
            raise GitOpError(
                "conflict",
                f"the merge would conflict{where}. Nothing was changed. Resolve it by "
                "editing the files so both sides agree, committing, and retrying, or ask the "
                "user to merge on the host.",
                {"conflicted": names},
            )
        return tree

    def _integrate(
        self, target: str, *, strategy: str, from_remote: bool, label: str, no_ff: bool
    ) -> dict[str, Any]:
        old = self._rev("HEAD")
        assert old is not None
        if target == old or self._is_ancestor(target, old):
            return {
                "ok": True, "strategy": strategy, "updated": None, "files_changed": [],
                "note": "already up to date", "branch": self.branch_summary(),
            }
        ff_possible = self._is_ancestor(old, target)
        opt_in = self.pull_outside_scope and from_remote and bool(self.scope)

        # ---- I8: compute the result and check it, touching nothing ----------
        if strategy == "ff-only":
            if not ff_possible:
                raise GitOpError(
                    "diverged",
                    "local and remote history have diverged, so a fast-forward is impossible. Use "
                    "git_pull(strategy=\"merge\") or git_merge.",
                )
            result = target
        elif strategy == "merge":
            result = target if (ff_possible and not no_ff) else self._merge_tree(old, target)
            if no_ff and ff_possible:
                result = target  # a --no-ff merge of a descendant has the target's tree
        else:  # rebase
            if ff_possible:
                result = target
            else:
                local = self._out(["rev-list", "--parents", f"{target}..{old}"]).splitlines()
                merges = [ln.split()[0][:7] for ln in local if len(ln.split()) > 2]
                if merges:
                    raise GitOpError(
                        "diverged",
                        f"unpushed merge commits ({_listing(merges)}) cannot be rebased; use "
                        "git_pull(strategy=\"merge\") instead.",
                    )
                host = []
                for ln in local:
                    sha = ln.split()[0]
                    if self._outside(self._commit_names(sha)[0]):
                        host.append(sha[:7])
                if host:
                    raise GitOpError(
                        "outside_scope_commits",
                        f"unpushed commits ({_listing(host)}) change files outside your folder; "
                        "rebasing would rewrite them. Use git_pull(strategy=\"merge\") instead.",
                        {"commits": host},
                    )
                # Every rewritten commit is scope-only, so the rebased result
                # differs from HEAD outside the scope only where `target` does,
                # verbatim.
                result = target

        d1 = self._outside(self._names(old, result))
        if opt_in:
            # The blob rule (plan, 'Opt-in'): an outside path must end up
            # identical to HEAD's (unchanged) or to the remote's (verbatim).
            d2 = set(self._outside(self._names(target, result)))
            violations = [p for p in d1 if p in d2]
        else:
            violations = d1
        if violations:
            if opt_in:
                msg = (
                    f"refusing: these files outside your folder have changes on both sides "
                    f"({_listing(violations)}), so the result would be content neither the remote "
                    "nor the host wrote. Ask the user to pull and resolve on the host."
                )
            elif self.scope:
                msg = (
                    f"refusing: this would change files outside your folder "
                    f"({_listing(violations)}). Nothing was changed. Ask the user to pull on the "
                    "host; until then pushes will be rejected as non-fast-forward."
                )
            else:
                msg = f"refusing: unexpected changes outside scope ({_listing(violations)})"
            raise GitOpError("outside_scope", msg, {"paths": violations})

        # ---- apply -----------------------------------------------------------
        with self._exclusive():
            self._check_uploads()   # an upload may have started while we computed
            self._apply(old, target, strategy=strategy, ff_possible=ff_possible, label=label, no_ff=no_ff)

        new = self._rev("HEAD")
        assert new is not None
        # Post-hoc verification, strict mode and opt-in alike. `merge` and
        # `merge-tree` share an implementation, so this should never fire;
        # it exists so that if it ever does, the operator hears about it.
        post = self._outside(self._names(old, new))
        if opt_in:
            post = [p for p in post if p in set(self._outside(self._names(target, new)))]
        if post:
            log.error("scope verification failed AFTER %s: %s (HEAD %s -> %s)", strategy, post, old, new)
            raise GitOpError(
                "git_failed",
                f"post-operation scope verification failed for {_listing(post)}. The repository is "
                f"at {new[:7]} (was {old[:7]}). Tell the user; this should not happen.",
            )
        names = self._names(old, new)
        mine = [self.scope_path(n) or n for n in names if self.in_scope(n)]
        outside = self._outside(names)
        res: dict[str, Any] = {
            "ok": True,
            "strategy": strategy,
            "updated": f"{_short(old)}..{_short(new)}",
            "files_changed": mine[:MAX_FILES_LISTED],
            "branch": self.branch_summary(),
        }
        if len(mine) > MAX_FILES_LISTED:
            res["files_changed_truncated"] = True
        if outside:
            res["outside_scope_files_changed"] = outside[:MAX_FILES_LISTED]
        return res

    def _apply(
        self, old: str, target: str, *, strategy: str, ff_possible: bool, label: str, no_ff: bool
    ) -> None:
        env = {"GIT_REFLOG_ACTION": f"claude-files {strategy}"}
        branch = self.branch
        try:
            if strategy == "ff-only" or (strategy == "rebase" and ff_possible) or (
                strategy == "merge" and ff_possible and not no_ff
            ):
                self._git(["merge", "--ff-only", "--no-edit", "--end-of-options", target],
                          write=True, env_extra=env)
            elif strategy == "merge":
                args = ["merge", "--no-edit", "-m", f"Merge {label} into {branch}"]
                if no_ff:
                    args.append("--no-ff")
                self._git([*args, "--end-of-options", target], write=True, env_extra=env)
            else:
                self._git(["rebase", "--no-autostash", "--end-of-options", target],
                          write=True, env_extra=env)
        except GitOpError as exc:
            if self._operation_in_progress() is not None:
                conflicted = self._split_names(
                    self._git(["diff", "--name-only", "--diff-filter=U", "-z"],
                              write=True, rc_ok=(0, 1))[1]
                )
                op = self._operation_in_progress()
                abort = ["rebase", "--abort"] if op == "rebase" else ["merge", "--abort"]
                try:
                    self._git(abort, write=True)
                except GitOpError:
                    log.error("could not abort an unexpected %s; the repository needs attention", op)
                if conflicted or exc.code == "conflict":
                    mine = [self.scope_path(n) or n for n in conflicted if self.in_scope(n)]
                    raise GitOpError(
                        "conflict",
                        f"the {op} conflicted ({_listing(mine) if mine else 'see git_status'}) and "
                        "was aborted: the working tree is back to how it was. Resolve the "
                        "difference and retry, or ask the user to merge on the host.",
                        {"conflicted": conflicted},
                    ) from exc
            raise
