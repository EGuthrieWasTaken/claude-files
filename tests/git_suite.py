#!/usr/bin/env python3
"""Tests for the git tools (docs/plan-mcp-git-ops.md).

Runs against REAL git (no mocks of git itself):

  python3 tests/git_suite.py            # all tests
  python3 tests/git_suite.py push_      # only tests whose name contains "push_"

This file covers app.git_ops directly. The MCP/REST surface (tool registration
per ENABLE_GIT_READ x ENABLE_GIT_WRITE combination, token scopes, error
mapping) is tests/git_surface_tests.py.

The in-process layer builds, per test, a throwaway world:

    origin.git/          bare "remote"
    repo/                the host's working tree  (GIT_REPO_ROOT)
      sub-folder-1/      the agent's folder       (DATA_ROOT, the *scope*)
      sub-folder-2/      outside the scope: visible to git_status, never writable
    other/               a second clone, standing in for "someone else pushed"

The remote is reached over git's *file* transport, which production refuses by
design (only ssh:// and https:// are allowed). The tests widen two module-level
seams in app.git_ops to allow it -- seams that are not reachable from an
environment variable or from an agent. What this therefore does NOT exercise is
the ssh transport and deploy-key plumbing; that needs an sshd, and is covered
by the container-level run in tests/run_tests.sh.

Every "must not" test asserts that nothing changed: HEAD, the index, and the
bytes of every file in the working tree are snapshotted before and compared
after.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# The suite imports app.* modules directly, never app.main, so nothing here
# needs API_TOKEN or a mount point.
from app import git_ops  # noqa: E402
from app.config import Config  # noqa: E402
from app.gate import WorktreeGate  # noqa: E402
from app.git_ops import GitOpError, GitRepo  # noqa: E402
from app.safepath import PathError, Vault  # noqa: E402

HOST_ENV = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "Host Person",
    "GIT_AUTHOR_EMAIL": "host@example.com",
    "GIT_COMMITTER_NAME": "Host Person",
    "GIT_COMMITTER_EMAIL": "host@example.com",
    "LC_ALL": "C",
}

PASSED: list[str] = []
FAILED: list[tuple[str, str]] = []
_TMPDIRS: list[str] = []


# ----------------------------------------------------------------------------
# harness
# ----------------------------------------------------------------------------


def sh(*args: str, cwd: str | Path, check: bool = True) -> str:
    p = subprocess.run(["git", *args], cwd=cwd, env=HOST_ENV, capture_output=True, text=True)
    if check and p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {p.stderr}")
    return p.stdout


def expect_err(code: str, fn: Callable[..., Any], *a: Any, **kw: Any) -> GitOpError:
    try:
        fn(*a, **kw)
    except GitOpError as exc:
        assert exc.code == code, f"expected {code}, got {exc.code}: {exc.message}"
        return exc
    raise AssertionError(f"expected GitOpError({code}), but the call succeeded")


class World:
    """origin.git + repo (host working tree) + other (someone else's clone)."""

    def __init__(self, *, scope_dir: str | None = "sub-folder-1", **cfg_overrides: Any) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="gitsuite-"))
        _TMPDIRS.append(str(self.tmp))
        self.origin = self.tmp / "origin.git"
        self.repo = self.tmp / "repo"
        self.other = self.tmp / "other"
        sh("init", "-q", "--bare", "-b", "main", str(self.origin), cwd=self.tmp)
        sh("clone", "-q", str(self.origin), str(self.repo), cwd=self.tmp)
        sh("checkout", "-q", "-b", "main", cwd=self.repo, check=False)
        self.write("README.md", "root readme\n")
        self.write("sub-folder-1/a.txt", "".join(f"a{i}\n" for i in range(1, 11)))
        self.write("sub-folder-1/notes/n.md", "notes\n")
        self.write("sub-folder-2/b.txt", "".join(f"b{i}\n" for i in range(1, 11)))
        sh("add", "-A", cwd=self.repo)
        sh("commit", "-q", "-m", "initial", cwd=self.repo)
        sh("push", "-q", "-u", "origin", "main", cwd=self.repo)
        sh("clone", "-q", str(self.origin), str(self.other), cwd=self.tmp)
        self.scope_dir = scope_dir
        self.cfg_overrides = cfg_overrides
        self.gate = WorktreeGate(shared_timeout=2.0)
        self._ops: GitRepo | None = None

    # -- host / other helpers ---------------------------------------------

    def write(self, rel: str, text: str, base: Path | None = None) -> None:
        p = (base or self.repo) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def host(self, *args: str, check: bool = True) -> str:
        return sh(*args, cwd=self.repo, check=check)

    def upstream(self, rel: str, text: str, msg: str = "upstream change") -> None:
        """Someone else commits a change and pushes it to origin."""
        sh("pull", "-q", "--no-rebase", "origin", "main", cwd=self.other)
        self.write(rel, text, base=self.other)
        sh("add", "-A", cwd=self.other)
        sh("commit", "-q", "-m", msg, cwd=self.other)
        sh("push", "-q", "origin", "main", cwd=self.other)

    def host_commit(self, rel: str, text: str, msg: str = "host commit") -> None:
        self.write(rel, text)
        self.host("add", "-A")
        self.host("commit", "-q", "-m", msg)

    # -- the system under test --------------------------------------------

    def ops(self, **overrides: Any) -> GitRepo:
        cfg = dict(
            token="t" * 16,
            root=str(self.repo / self.scope_dir) if self.scope_dir else str(self.repo),
            git_repo_root=str(self.repo),
            require_mount=False,
            enable_git_read=True,
            enable_git_write=True,
            git_remote="origin",
            git_branch="main",
            git_author_name="claude-files agent",
            git_author_email="agent@example.invalid",
            denied_names=frozenset({".git", ".gitmodules", ".ssh"}),
            git_local_timeout_seconds=30,
            git_network_timeout_seconds=30,
        )
        cfg.update(self.cfg_overrides)
        cfg.update(overrides)
        uploads = cfg.pop("_uploads", None)
        config = Config(**cfg)
        vault = Vault(config.root, denied_names=config.denied_names, gate=self.gate)
        ops = GitRepo(config, vault=vault, gate=self.gate, uploads_in_flight=uploads)
        ops.start()
        self._ops = ops
        return ops

    # -- snapshots ----------------------------------------------------------

    def snap(self) -> dict[str, Any]:
        files: dict[str, str] = {}
        for root, dirs, names in os.walk(self.repo):
            dirs[:] = [d for d in dirs if d != ".git"]
            for n in names:
                p = Path(root) / n
                files[str(p.relative_to(self.repo))] = hashlib.sha1(p.read_bytes()).hexdigest()
        return {
            "head": self.host("rev-parse", "HEAD").strip(),
            "index": self.host("ls-files", "-s", "-z"),
            "files": files,
            "ops": [n for n in (".git/MERGE_HEAD", ".git/rebase-merge", ".git/rebase-apply")
                    if (self.repo / n).exists()],
        }

    def assert_unchanged(self, before: dict[str, Any], what: str = "") -> None:
        after = self.snap()
        for key in ("head", "index", "files", "ops"):
            assert before[key] == after[key], f"{what}: {key} changed:\n{before[key]!r}\n->\n{after[key]!r}"


def changed_in_head(w: World, rev: str = "HEAD") -> set[str]:
    out = w.host("diff-tree", "-r", "--no-commit-id", "--name-only", "-m", "--first-parent", rev)
    return {ln for ln in out.splitlines() if ln}


def test(fn: Callable[[], None]) -> Callable[[], None]:
    fn._is_test = True  # type: ignore[attr-defined]
    return fn


# ----------------------------------------------------------------------------
# status / log / diff (read tools)
# ----------------------------------------------------------------------------


def _read_bytes(path: str) -> bytes:
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return b""


@test
def status_lists_whole_repo_with_scope_flags() -> None:
    w = World()
    g = w.ops()
    w.write("sub-folder-1/a.txt", "changed\n")                    # in scope, unstaged
    w.write("sub-folder-2/new.txt", "x\n")                        # outside, untracked
    w.host("mv", "sub-folder-2/b.txt", "sub-folder-2/b2.txt")     # outside, staged rename
    w.write("sub-folder-1/.upload-abc.tmp", "half")               # in-flight upload: hidden
    st = g.status()
    paths = {c["path"]: c for c in st["changes"]}
    assert ".upload-abc.tmp" not in str(st), "upload temp file leaked into status"
    assert paths["sub-folder-1/a.txt"]["in_scope"] and paths["sub-folder-1/a.txt"]["scope_path"] == "a.txt"
    assert paths["sub-folder-1/a.txt"]["worktree"] == "M"
    assert not paths["sub-folder-2/new.txt"]["in_scope"] and paths["sub-folder-2/new.txt"]["scope_path"] is None
    ren = paths["sub-folder-2/b2.txt"]
    assert ren["from"] == "sub-folder-2/b.txt" and ren["index"] == "R" and not ren["in_scope"]
    assert st["summary"] == {"in_scope": 1, "outside_scope": 2, "conflicted": 0}, st["summary"]
    assert st["changes"][0]["in_scope"], "in-scope entries must be listed first"
    assert st["scope"] == "sub-folder-1/"
    b = st["branch"]
    assert b["name"] == "main" and b["upstream"] == "origin/main" and (b["ahead"], b["behind"]) == (0, 0)


@test
def status_limit_drops_outside_noise_first() -> None:
    w = World()
    g = w.ops()
    for i in range(5):
        w.write(f"sub-folder-2/n{i}.txt", "x")
    w.write("sub-folder-1/mine.txt", "x")
    st = g.status(limit=1)
    assert st["truncated"] and len(st["changes"]) == 1 and st["changes"][0]["path"] == "sub-folder-1/mine.txt"
    assert st["summary"]["outside_scope"] == 5, "summary must carry the full counts"


@test
def status_never_takes_the_index_lock() -> None:
    w = World()
    g = w.ops()
    (w.repo / ".git" / "index.lock").write_text("")   # a host git "mid-operation"
    try:
        g.status()                                    # read tools must not care
    finally:
        (w.repo / ".git" / "index.lock").unlink()


@test
def diff_is_scope_only_staged_and_unstaged() -> None:
    w = World()
    g = w.ops()
    w.write("sub-folder-1/a.txt", "inside\n")
    w.write("sub-folder-2/b.txt", "OUTSIDE-SECRET\n")
    w.host("add", "sub-folder-2/b.txt")
    d = g.diff()
    assert "inside" in d["diff"] and "OUTSIDE-SECRET" not in d["diff"] and "sub-folder-2" not in d["diff"]
    w.host("add", "sub-folder-1/a.txt")
    s = g.diff(staged=True)
    assert "inside" in s["diff"] and "OUTSIDE-SECRET" not in s["diff"]
    assert "sub-folder-2" not in g.diff(path="a.txt")["diff"]
    for bad in ("../sub-folder-2/b.txt", "/etc/passwd", ".git/config", "a/../../x"):
        try:
            g.diff(path=bad)
        except PathError:
            continue
        raise AssertionError(f"diff accepted path {bad!r}")


@test
def diff_pages_like_read_file() -> None:
    w = World()
    g = w.ops()
    w.write("sub-folder-1/a.txt", "".join(f"line {i}\n" for i in range(500)))
    full = g.diff(inline_limit=10**7)["diff"]
    got, off = "", 0
    while True:
        r = g.diff(offset=off, inline_limit=300)
        got += r["diff"]
        off += len(r["diff"])
        if not r["truncated"]:
            break
        assert "offset=" in r["note"]
    assert got == full


@test
def log_flags_commits_that_touch_outside_the_scope() -> None:
    w = World()
    g = w.ops()
    w.write("sub-folder-1/a.txt", "x\n")
    w.write("sub-folder-2/b.txt", "y\n")
    w.host("add", "-A")
    w.host("commit", "-q", "-m", "both folders")
    w.host_commit("sub-folder-1/n.txt", "n", "scope only")
    cs = g.log(limit=5)["commits"]
    assert [c["subject"] for c in cs[:3]] == ["scope only", "both folders", "initial"]
    assert [c["touches_outside_scope"] for c in cs[:3]] == [False, True, True]
    only = g.log(limit=5, scope_only=True)["commits"]
    assert all(c["subject"] != "x" for c in only) and len(only) == 3


# ----------------------------------------------------------------------------
# commit
# ----------------------------------------------------------------------------


@test
def commit_only_takes_scope_and_leaves_host_staging_alone() -> None:
    w = World()
    g = w.ops(git_commit_trailer="Committed-Via: claude-files")
    w.write("sub-folder-1/a.txt", "edited\n")
    w.write("sub-folder-1/new.txt", "new\n")
    w.write("sub-folder-1/.upload-zz.tmp", "half-written")       # must never be committed
    w.write("sub-folder-2/b.txt", "host edit\n")
    w.host("add", "sub-folder-2/b.txt")                          # a person staged this on the host
    r = g.commit("agent: update notes")
    assert r["ok"] and r["files_changed"] == 2
    assert changed_in_head(w) == {"sub-folder-1/a.txt", "sub-folder-1/new.txt"}, changed_in_head(w)
    assert w.host("log", "-1", "--format=%an <%ae>|%cn").strip() == "claude-files agent <agent@example.invalid>|claude-files agent"
    body = w.host("log", "-1", "--format=%B")
    assert body.startswith("agent: update notes") and body.rstrip().endswith("Committed-Via: claude-files")
    status = w.host("status", "--porcelain")
    assert "M  sub-folder-2/b.txt" in status, "the host's staged change must stay staged:\n" + status
    assert "sub-folder-1/a.txt" not in status.replace("??", ""), "scope should read clean after a commit"
    assert ".upload-zz.tmp" in status, "the temp upload must remain untracked"
    assert r["outside_scope_changes_left"] == 1
    assert (w.repo / "sub-folder-2/b.txt").read_text() == "host edit\n"


@test
def commit_message_starting_with_dash_is_just_a_message() -> None:
    w = World()
    g = w.ops()
    w.write("sub-folder-1/a.txt", "x\n")
    g.commit("--amend --no-verify -m pwned")
    assert w.host("log", "-1", "--format=%s").strip() == "--amend --no-verify -m pwned"
    assert w.host("rev-list", "--count", "HEAD").strip() == "2"


@test
def commit_explicit_paths_and_deletions() -> None:
    w = World()
    g = w.ops()
    w.write("sub-folder-1/a.txt", "edited\n")
    w.write("sub-folder-1/notes/n.md", "edited\n")
    g.commit("only notes", paths=["notes/n.md"])
    assert changed_in_head(w) == {"sub-folder-1/notes/n.md"}
    assert "a.txt" in w.host("status", "--porcelain")
    (w.repo / "sub-folder-1/a.txt").unlink()
    g.commit("remove a", paths=["a.txt"])
    assert changed_in_head(w) == {"sub-folder-1/a.txt"}
    assert not (w.repo / "sub-folder-1/a.txt").exists()


def _explicit_paths_commit_untracked(w: World, base: str) -> None:
    g = w.ops()
    w.write(f"{base}a.txt", "edited\n") if base else w.write("README.md", "edited\n")
    tracked = f"{base}a.txt" if base else "README.md"
    rel = lambda p: p[len(base):]  # noqa: E731  (scope-relative form of a repo path)
    w.write(f"{base}briefs/2026/x.pdf", "new\n")       # new file in folders that do not exist yet
    w.write(f"{base}top-new.md", "new top\n")           # new file in an existing folder
    res = g.commit("tracked + untracked",
                   paths=[rel(tracked), "briefs/2026/x.pdf", "top-new.md"])
    assert res["files_changed"] == 3, res
    assert not res.get("skipped"), res
    assert changed_in_head(w) == {tracked, f"{base}briefs/2026/x.pdf", f"{base}top-new.md"}
    assert w.host("status", "--porcelain").strip() == "", w.host("status", "--porcelain")
    # a brand-new SUBFOLDER named on its own is accepted as a path too
    w.write(f"{base}more/y.md", "y\n")
    res = g.commit("folder path", paths=["more"])
    assert res["files_changed"] == 1 and changed_in_head(w) == {f"{base}more/y.md"}


@test
def commit_explicit_paths_include_new_untracked_files() -> None:
    """Reported bug: paths=[modified tracked file, brand-new file] committed
    only the tracked file, with ok:true. Scoped folder."""
    _explicit_paths_commit_untracked(World(), "sub-folder-1/")


@test
def commit_explicit_paths_include_new_untracked_files_whole_repo_scope() -> None:
    """Same, with scope = the whole repository (empty prefix). This is the
    layout where git 2.47's `git add -A` dropped untracked files whenever an
    ':(exclude,glob).upload-*.tmp' pathspec rode along."""
    _explicit_paths_commit_untracked(World(scope_dir=None), "")


@test
def commit_new_file_beside_tracked_files_is_committed_with_explicit_paths() -> None:
    """The reported shape exactly: daily-briefs/news-log.md (tracked, modified)
    plus daily-briefs/2026/x.pdf, a new file in a folder that already holds
    tracked files. This is the shape git 2.47 got wrong in whole-repo scope."""
    for scope_dir, base in (("sub-folder-1", "sub-folder-1/"), (None, "")):
        w = World(scope_dir=scope_dir)
        w.host_commit(f"{base}daily-briefs/news-log.md", "log\n")
        w.host_commit(f"{base}daily-briefs/2026/old.pdf", "old\n")
        g = w.ops()
        w.write(f"{base}daily-briefs/news-log.md", "log\nmore\n")
        (w.repo / f"{base}daily-briefs/2026/x.pdf").write_bytes(b"%PDF-1.4\n\x00\xff binary\n")
        res = g.commit("brief", paths=["daily-briefs/news-log.md", "daily-briefs/2026/x.pdf"])
        assert res["files_changed"] == 2 and not res.get("skipped"), res
        assert changed_in_head(w) == {f"{base}daily-briefs/news-log.md", f"{base}daily-briefs/2026/x.pdf"}
        assert w.host("status", "--porcelain").strip() == ""


@test
def commit_add_carries_only_the_slash_star_star_upload_exclude() -> None:
    """Version-independent guard for the bug above, which only misbehaves on
    some git versions: assert the cause directly. The only exclude spec an
    `add` may carry is '<scope>**/.upload-*.tmp'; a second, prefix-only
    ':(exclude,glob)<scope>.upload-*.tmp' made git 2.47 skip untracked files."""
    for scope_dir in ("sub-folder-1", None):
        w = World(scope_dir=scope_dir)
        g = w.ops()
        seen: list[list[str]] = []
        real = g._git

        def spy(args: list[str], *a: Any, **kw: Any) -> Any:
            seen.append(list(args))
            return real(args, *a, **kw)

        g._git = spy  # type: ignore[method-assign]
        base = "sub-folder-1/" if scope_dir else ""
        w.write(f"{base}fresh/new.md", "n\n")
        g.commit("c", paths=["fresh/new.md"])
        w.write(f"{base}fresh/new2.md", "n\n")
        g.commit("c2")
        excludes = [x for a in seen if a and a[0] == "add" for x in a if "exclude" in x]
        assert excludes, seen
        assert all(x.endswith("**/.upload-*.tmp") for x in excludes), excludes


@test
def commit_reports_requested_paths_that_added_nothing() -> None:
    w = World()
    g = w.ops()
    w.write("sub-folder-1/a.txt", "edited\n")
    res = g.commit("one real, two empty", paths=["a.txt", "notes/n.md", "ghost.md"])
    assert res["ok"] and res["files_changed"] == 1, res
    assert sorted((x["path"], x["reason"]) for x in res["skipped"]) == [
        ("ghost.md", "not_found"), ("notes/n.md", "unchanged")], res
    assert "skipped" in res["note"]
    # nothing at all to commit is still the hard error, not a skipped list
    expect_err("nothing_to_commit", g.commit, "none", paths=["notes/n.md", "ghost.md"])


@test
def commit_leaves_in_flight_upload_temp_files_out_in_every_layout() -> None:
    for scope_dir, base in (("sub-folder-1", "sub-folder-1/"), (None, "")):
        w = World(scope_dir=scope_dir)
        g = w.ops()
        w.write(f"{base}docs/.upload-abc123.tmp", "half\n")
        w.write(f"{base}.upload-root.tmp", "half\n")
        w.write(f"{base}docs/real.md", "real\n")
        res = g.commit("with tmp lying around")                         # no paths
        assert changed_in_head(w) == {f"{base}docs/real.md"}, changed_in_head(w)
        assert not res.get("skipped")
        w.write(f"{base}docs/real2.md", "real2\n")
        res = g.commit("explicit", paths=["docs/real2.md", "docs/.upload-abc123.tmp"])
        assert changed_in_head(w) == {f"{base}docs/real2.md"}, changed_in_head(w)
        assert res["skipped"] == [{"path": "docs/.upload-abc123.tmp", "reason": "upload_in_flight"}], res
        # the temp files are still untracked and unstaged afterwards
        st = w.host("status", "--porcelain")
        assert "?? " in st and ".upload-abc123.tmp" in st and "A " not in st, st


@test
def commit_paths_cannot_widen_or_leave_the_scope() -> None:
    w = World()
    g = w.ops()
    w.write("sub-folder-1/a.txt", "edited\n")
    w.write("sub-folder-2/b.txt", "host edit\n")
    before = w.snap()
    for bad in ("../sub-folder-2/b.txt", "/abs/path", ".git/HEAD", "a/../../sub-folder-2/b.txt"):
        try:
            g.commit("x", paths=[bad])
        except PathError:
            continue
        raise AssertionError(f"commit accepted path {bad!r}")
    # pathspec magic is neutralised by :(literal): these name files that don't
    # exist INSIDE the scope, so nothing is selected -- and nothing leaks out.
    for magic in ("*", ":(top)sub-folder-2/b.txt", ":/sub-folder-2", "**"):
        try:
            g.commit("x", paths=[magic])
        except GitOpError:
            pass
    w.assert_unchanged(before, "commit with hostile paths")
    for empty in ([], ""):
        expect_err("invalid_argument", g.commit, "x", paths=empty)  # type: ignore[arg-type]


@test
def commit_nothing_to_commit_mentions_outside_changes() -> None:
    w = World()
    g = w.ops()
    w.write("sub-folder-2/b.txt", "host edit\n")
    exc = expect_err("nothing_to_commit", g.commit, "nothing")
    assert "outside your folder" in exc.message and "1" in exc.message


@test
def commit_runaway_guard_leaves_nothing_behind() -> None:
    w = World()
    g = w.ops(git_max_changed_files=3)
    for i in range(6):
        w.write(f"sub-folder-1/f{i}.txt", "x")
    before = w.snap()
    expect_err("too_many_changes", g.commit, "mass change")
    w.assert_unchanged(before, "runaway guard")
    assert not list(Path(g._state_dir).glob("index.*")), "temp index not cleaned up"
    g.commit("narrower", paths=["f0.txt"])      # the stated remedy works


@test
def commit_refuses_message_abuse() -> None:
    w = World()
    g = w.ops()
    w.write("sub-folder-1/a.txt", "x\n")
    for bad in ("", "   \n", "has\0nul", "x" * 5001):
        expect_err("invalid_argument", g.commit, bad)
    expect_err("invalid_argument", g.commit, 5)  # type: ignore[arg-type]


@test
def commit_refuses_after_host_changes_branch_or_mid_operation() -> None:
    w = World()
    g = w.ops()
    w.write("sub-folder-1/a.txt", "x\n")
    w.host("checkout", "-q", "-b", "elsewhere")
    expect_err("wrong_branch", g.commit, "x")
    w.host("checkout", "-q", "--detach")
    expect_err("detached_head", g.commit, "x")
    w.host("checkout", "-q", "main")
    (w.repo / ".git" / "MERGE_HEAD").write_text(w.host("rev-parse", "HEAD"))
    expect_err("operation_in_progress", g.commit, "x")


@test
def commit_works_when_scope_is_the_whole_repo() -> None:
    w = World(scope_dir=None)
    g = w.ops()
    w.write("README.md", "new root\n")
    w.write("sub-folder-2/b.txt", "anywhere\n")
    g.commit("everything")
    assert changed_in_head(w) == {"README.md", "sub-folder-2/b.txt"}
    assert g.status()["scope"] == "."


# ----------------------------------------------------------------------------
# push
# ----------------------------------------------------------------------------


@test
def push_fast_forward_then_up_to_date() -> None:
    w = World()
    g = w.ops()
    w.write("sub-folder-1/a.txt", "x\n")
    g.commit("agent change")
    r = g.push()
    assert r["ok"] and r["commits"] == 1 and r["pushed"]
    assert sh("rev-parse", "main", cwd=w.origin).strip() == w.host("rev-parse", "HEAD").strip()
    assert r["branch"]["ahead"] == 0
    again = g.push()
    assert again["pushed"] is None and again["commits"] == 0


@test
def push_never_forces_and_says_to_pull() -> None:
    w = World()
    g = w.ops()
    w.upstream("sub-folder-1/up.txt", "up\n")
    w.write("sub-folder-1/a.txt", "x\n")
    g.commit("agent change")
    remote_before = sh("rev-parse", "main", cwd=w.origin).strip()
    exc = expect_err("non_fast_forward", g.push)
    assert "git_pull" in exc.message
    assert sh("rev-parse", "main", cwd=w.origin).strip() == remote_before, "remote must be untouched"


@test
def push_refuses_host_commits_that_touch_outside_the_scope() -> None:
    w = World()
    g = w.ops()
    w.host_commit("sub-folder-2/b.txt", "host authored\n", "host edits folder 2")
    w.write("sub-folder-1/a.txt", "x\n")
    g.commit("agent change")                 # an innocent agent commit on top
    remote_before = sh("rev-parse", "main", cwd=w.origin).strip()
    exc = expect_err("outside_scope_commits", g.push)
    assert "host edits folder 2" in exc.message and "sub-folder-2/b.txt" in exc.message
    assert sh("rev-parse", "main", cwd=w.origin).strip() == remote_before


@test
def push_checks_every_commit_not_the_net_diff() -> None:
    w = World()
    g = w.ops()
    original = (w.repo / "sub-folder-2/b.txt").read_text()
    w.host_commit("sub-folder-2/b.txt", "temporarily different\n", "host: edit")
    w.host_commit("sub-folder-2/b.txt", original, "host: revert")     # net diff on folder 2 is empty
    remote_before = sh("rev-parse", "main", cwd=w.origin).strip()
    expect_err("outside_scope_commits", g.push)
    assert sh("rev-parse", "main", cwd=w.origin).strip() == remote_before


@test
def push_scope_check_ignores_sibling_prefix_names() -> None:
    w = World()
    g = w.ops()
    w.host_commit("sub-folder-10/x.txt", "sibling with a shared prefix\n", "host: sibling")
    expect_err("outside_scope_commits", g.push)


@test
def push_needs_a_fetch_when_the_tracking_ref_is_gone() -> None:
    w = World()
    g = w.ops()
    w.host("update-ref", "-d", "refs/remotes/origin/main")
    expect_err("stale_remote", g.push)
    g.fetch()
    assert g.push()["pushed"] is None


# ----------------------------------------------------------------------------
# fetch / pull
# ----------------------------------------------------------------------------


@test
def fetch_updates_tracking_ref_only() -> None:
    w = World()
    g = w.ops()
    w.upstream("sub-folder-1/up.txt", "up\n")
    before = w.snap()
    r = g.fetch()
    assert r["updated_refs"] and r["branch"]["behind"] == 1
    w.assert_unchanged(before, "fetch")


@test
def pull_ff_only_updates_scope_files() -> None:
    w = World()
    g = w.ops()
    w.upstream("sub-folder-1/a.txt", "from upstream\n")
    r = g.pull()
    assert r["ok"] and r["files_changed"] == ["a.txt"] and "updated" in r
    assert (w.repo / "sub-folder-1/a.txt").read_text() == "from upstream\n"
    assert g.pull()["note"] == "already up to date"


@test
def pull_refuses_upstream_changes_outside_the_scope_and_changes_nothing() -> None:
    w = World()
    g = w.ops()
    w.upstream("sub-folder-2/b.txt", "upstream edit to folder 2\n")
    w.upstream("sub-folder-1/a.txt", "upstream edit to scope\n")
    before = w.snap()
    for strategy in ("ff-only", "merge", "rebase"):
        exc = expect_err("outside_scope", g.pull, strategy)
        assert "sub-folder-2/b.txt" in exc.message and "host" in exc.message
        w.assert_unchanged(before, f"pull({strategy})")
    assert (w.repo / "sub-folder-1/a.txt").read_text().startswith("a1"), "scope file was touched"


@test
def pull_diverged_strategies() -> None:
    w = World()
    g = w.ops()
    w.upstream("sub-folder-1/up.txt", "up\n")
    w.write("sub-folder-1/a.txt", "local\n")
    g.commit("local change")
    expect_err("diverged", g.pull)
    r = g.pull("merge")
    assert r["ok"] and len(w.host("rev-list", "--parents", "-n1", "HEAD").split()) == 3, "expected a merge commit"
    assert (w.repo / "sub-folder-1/up.txt").exists()
    assert g.push()["ok"], "a merge of scope-only content must be pushable"


@test
def pull_rebase_linearises_local_commits() -> None:
    w = World()
    g = w.ops()
    w.upstream("sub-folder-1/up.txt", "up\n")
    w.write("sub-folder-1/a.txt", "local\n")
    g.commit("local change")
    old_head = w.host("rev-parse", "HEAD").strip()
    r = g.pull("rebase")
    assert r["ok"]
    assert len(w.host("rev-list", "--parents", "-n1", "HEAD").split()) == 2, "rebase must not create a merge"
    assert w.host("rev-parse", "HEAD").strip() != old_head
    assert w.host("log", "-1", "--format=%s").strip() == "local change"
    assert g.push()["ok"]


@test
def merge_conflict_is_detected_before_anything_changes() -> None:
    w = World()
    g = w.ops()
    w.upstream("sub-folder-1/a.txt", "upstream version\n")
    w.write("sub-folder-1/a.txt", "local version\n")
    g.commit("local conflicting change")
    before = w.snap()
    exc = expect_err("conflict", g.pull, "merge")
    assert "a.txt" in exc.message
    w.assert_unchanged(before, "conflicting merge")
    assert (w.repo / "sub-folder-1/a.txt").read_text() == "local version\n"
    assert "<<<<<<<" not in (w.repo / "sub-folder-1/a.txt").read_text()


@test
def rebase_conflict_is_aborted_and_the_tree_restored() -> None:
    w = World()
    g = w.ops()
    w.upstream("sub-folder-1/a.txt", "upstream version\n")
    w.write("sub-folder-1/a.txt", "local version\n")
    g.commit("local conflicting change")
    before = w.snap()
    expect_err("conflict", g.pull, "rebase")
    w.assert_unchanged(before, "conflicting rebase")
    assert not (w.repo / ".git/rebase-merge").exists() and not (w.repo / ".git/rebase-apply").exists()
    assert w.host("symbolic-ref", "--short", "HEAD").strip() == "main"


@test
def rebase_refuses_to_rewrite_host_commits_touching_outside() -> None:
    w = World()
    g = w.ops()
    w.upstream("sub-folder-1/up.txt", "up\n")
    w.host_commit("sub-folder-2/b.txt", "host edit\n", "host: folder 2")
    before = w.snap()
    expect_err("outside_scope_commits", g.pull, "rebase")
    w.assert_unchanged(before, "rebase over a host commit")
    r = g.pull("merge")            # the suggested alternative works
    assert r["ok"]


@test
def pull_reports_dirty_files_split_by_who_can_fix_them() -> None:
    w = World()
    g = w.ops()
    # A person staged a change in sub-folder-2 on the host: git refuses merges
    # while the index differs from HEAD, even for files the merge doesn't touch.
    w.upstream("sub-folder-1/up.txt", "up\n")
    w.write("sub-folder-1/a.txt", "local\n")
    g.commit("local change")
    w.write("sub-folder-2/b.txt", "host staged\n")
    w.host("add", "sub-folder-2/b.txt")
    before = w.snap()
    exc = expect_err("dirty_worktree", g.pull, "merge")
    assert "sub-folder-2/b.txt" in exc.message and "person on the host" in exc.message
    w.assert_unchanged(before, "merge blocked by staged outside change")
    # An unstaged edit to an in-scope file that upstream also changes is the
    # agent's own to commit.
    w2 = World()
    g2 = w2.ops()
    w2.upstream("sub-folder-1/a.txt", "upstream\n")
    w2.write("sub-folder-1/a.txt", "uncommitted local edit\n")
    exc2 = expect_err("dirty_worktree", g2.pull)
    assert "git_commit" in exc2.message and "a.txt" in exc2.message
    assert (w2.repo / "sub-folder-1/a.txt").read_text() == "uncommitted local edit\n"


@test
def pull_and_merge_refuse_while_uploads_are_in_flight() -> None:
    w = World()
    busy = ["reports/big.bin"]
    g = w.ops(_uploads=lambda: busy)
    w.upstream("sub-folder-1/up.txt", "up\n")
    before = w.snap()
    exc = expect_err("uploads_in_flight", g.pull)
    assert "reports/big.bin" in exc.message
    w.host("branch", "feature")
    expect_err("uploads_in_flight", g.merge, "feature")
    w.assert_unchanged(before, "pull during upload")
    busy.clear()
    assert g.pull()["ok"]
    # commit and push never needed the guard
    busy.append("x")
    w.write("sub-folder-1/a.txt", "x\n")
    assert g.commit("c")["ok"] and g.push()["ok"]


# ----------------------------------------------------------------------------
# merge
# ----------------------------------------------------------------------------


@test
def merge_local_and_remote_tracking_branches() -> None:
    w = World()
    g = w.ops()
    w.host("checkout", "-q", "-b", "feature")
    w.host_commit("sub-folder-1/feature.txt", "f\n", "feature work")
    w.host("checkout", "-q", "main")
    w.write("sub-folder-1/a.txt", "main edit\n")
    g.commit("main work")
    r = g.merge("feature")
    assert r["ok"] and (w.repo / "sub-folder-1/feature.txt").exists()
    assert len(w.host("rev-list", "--parents", "-n1", "HEAD").split()) == 3
    # a descendant merges as a fast-forward unless no_ff is given
    w.host("checkout", "-q", "-b", "ff", "main")
    w.host_commit("sub-folder-1/ff.txt", "f\n", "ff work")
    w.host("checkout", "-q", "main")
    g.merge("ff", no_ff=True)
    assert len(w.host("rev-list", "--parents", "-n1", "HEAD").split()) == 3, "no_ff must make a merge commit"
    # remote-tracking
    w.upstream("sub-folder-1/up.txt", "up\n")
    g.fetch()
    assert g.merge("origin/main")["ok"] and (w.repo / "sub-folder-1/up.txt").exists()


@test
def merge_refuses_a_local_branch_that_touches_outside_the_scope() -> None:
    w = World()
    g = w.ops()
    w.host("checkout", "-q", "-b", "feature")
    w.host_commit("sub-folder-2/b.txt", "feature edit\n", "feature: folder 2")
    w.host("checkout", "-q", "main")
    w.write("sub-folder-1/a.txt", "x\n")
    g.commit("diverge")
    before = w.snap()
    expect_err("outside_scope", g.merge, "feature")
    w.assert_unchanged(before, "merge of a branch touching folder 2")


@test
def merge_ref_injection_is_refused() -> None:
    w = World()
    g = w.ops()
    w.host("branch", "feature")
    w.host("tag", "v1")
    w.host("update-ref", "refs/remotes/other-remote/main", "HEAD")
    canary = w.tmp / "canary"
    hostile = [
        f"--upload-pack=touch {canary}", "-X ours", "--no-ff", "-s", "HEAD@{1}", ":/foo", "HEAD~1",
        "feature^", "feature~1", "../x", "refs/tags/v1", "v1", "other-remote/main", "main",
        "a" * 300, "feature\0x", "feature extra", "fea ture", "", "-", "feature..main", "feature.lock",
        "origin/", "origin", w.host("rev-parse", "HEAD").strip(), "feature\n--abort",
    ]
    before = w.snap()
    for ref in hostile:
        try:
            g.merge(ref)
        except GitOpError as exc:
            assert exc.code == "invalid_ref", f"{ref!r}: {exc.code}: {exc.message}"
        else:
            raise AssertionError(f"merge accepted {ref!r}")
    for bad in (None, 5, ["feature"]):
        expect_err("invalid_ref", g.merge, bad)  # type: ignore[arg-type]
    assert not canary.exists()
    w.assert_unchanged(before, "hostile merge refs")


# ----------------------------------------------------------------------------
# GIT_PULL_OUTSIDE_SCOPE (the opt-in)
# ----------------------------------------------------------------------------


@test
def opt_in_pulls_remote_content_outside_the_scope_verbatim() -> None:
    w = World()
    g = w.ops(git_pull_outside_scope=True)
    w.upstream("sub-folder-2/b.txt", "upstream edit to folder 2\n")
    w.upstream("sub-folder-1/a.txt", "upstream edit to scope\n")
    r = g.pull()
    assert r["ok"] and r["files_changed"] == ["a.txt"]
    assert r["outside_scope_files_changed"] == ["sub-folder-2/b.txt"]
    assert (w.repo / "sub-folder-2/b.txt").read_text() == "upstream edit to folder 2\n"
    assert w.host("rev-parse", "HEAD").strip() == sh("rev-parse", "main", cwd=w.origin).strip()


@test
def opt_in_refuses_content_that_neither_side_wrote() -> None:
    w = World()
    g = w.ops(git_pull_outside_scope=True)
    host_text = (w.repo / "sub-folder-2/b.txt").read_text().replace("b1\n", "HOST1\n")
    up_text = (w.repo / "sub-folder-2/b.txt").read_text().replace("b10\n", "UP10\n")
    w.host_commit("sub-folder-2/b.txt", host_text, "host edits line 1")
    w.upstream("sub-folder-2/b.txt", up_text)          # upstream edits line 10: merges cleanly...
    before = w.snap()
    exc = expect_err("outside_scope", g.pull, "merge")  # ...but into a blob neither side wrote
    assert "both sides" in exc.message and "sub-folder-2/b.txt" in exc.message
    w.assert_unchanged(before, "three-way merge outside scope")


@test
def opt_in_does_not_apply_to_local_branches() -> None:
    w = World()
    g = w.ops(git_pull_outside_scope=True)
    w.host("checkout", "-q", "-b", "feature")
    w.host_commit("sub-folder-2/b.txt", "feature edit\n", "feature: folder 2")
    w.host("checkout", "-q", "main")
    w.write("sub-folder-1/a.txt", "x\n")
    g.commit("diverge")
    expect_err("outside_scope", g.merge, "feature")


@test
def opt_in_never_overwrites_a_hosts_uncommitted_edit() -> None:
    w = World()
    g = w.ops(git_pull_outside_scope=True)
    w.upstream("sub-folder-2/b.txt", "upstream\n")
    w.write("sub-folder-2/b.txt", "a person is editing this\n")
    exc = expect_err("dirty_worktree", g.pull)
    assert "sub-folder-2/b.txt" in exc.message and "host" in exc.message
    assert (w.repo / "sub-folder-2/b.txt").read_text() == "a person is editing this\n"


@test
def opt_in_merge_commit_is_pushable_but_host_commits_still_are_not() -> None:
    w = World()
    g = w.ops(git_pull_outside_scope=True)
    w.upstream("sub-folder-2/b.txt", "upstream edit\n")
    w.write("sub-folder-1/a.txt", "local\n")
    g.commit("local change")
    assert g.pull("merge")["ok"]
    assert g.push()["ok"], "a merge whose outside content is verbatim from the remote must push"
    # ...but opt-in never lets the agent publish a host-authored outside commit
    w.host_commit("sub-folder-2/b.txt", "host authored\n", "host: folder 2")
    expect_err("outside_scope_commits", g.push)


@test
def opt_in_rebase_still_refuses_host_commits_outside() -> None:
    w = World()
    g = w.ops(git_pull_outside_scope=True)
    w.upstream("sub-folder-1/up.txt", "up\n")
    w.host_commit("sub-folder-2/b.txt", "host edit\n", "host: folder 2")
    expect_err("outside_scope_commits", g.pull, "rebase")


@test
def opt_in_requires_a_writable_root_mount_and_is_inert_for_whole_repo_scope() -> None:
    real = os.statvfs

    class RO:
        def __init__(self, st: os.statvfs_result) -> None:
            self.f_flag = st.f_flag | os.ST_RDONLY

    git_ops.os.statvfs = lambda p: RO(real(p))      # type: ignore[assignment]
    try:
        w = World()
        try:
            w.ops(git_pull_outside_scope=True)
        except RuntimeError as exc:
            assert "read-write" in str(exc)
        else:
            raise AssertionError("opt-in accepted on a read-only root mount")
        # ...and a read-only root with write mode off is fine, reported as 'kernel'
        # (the .git/DATA_ROOT writable probe only applies to write mode)
        g = w.ops(enable_git_write=False)
        assert g.write_boundary == "kernel"
        w2 = World(scope_dir=None)
        assert w2.ops(enable_git_write=False, git_pull_outside_scope=True).write_boundary == "n/a"
    finally:
        git_ops.os.statvfs = real                      # type: ignore[assignment]


@test
def writable_root_is_a_warning_not_a_refusal() -> None:
    w = World()
    g = w.ops()
    assert g.write_boundary == "software"
    assert g.info_block()["write_boundary"] == "software"


# ----------------------------------------------------------------------------
# git as an escape route
# ----------------------------------------------------------------------------


def _canary_script(w: World, name: str) -> tuple[Path, str]:
    canary = w.tmp / f"CANARY-{name}"
    script = w.tmp / f"{name}.sh"
    script.write_text(f"#!/bin/sh\ntouch '{canary}'\ncat >/dev/null 2>&1\nexit 0\n")
    script.chmod(0o755)
    return canary, str(script)


@test
def planted_hooks_never_run() -> None:
    w = World()
    canaries = []
    for hook in ("pre-commit", "commit-msg", "post-commit", "post-merge", "pre-merge-commit",
                 "pre-push", "reference-transaction", "post-checkout", "post-rewrite", "pre-rebase"):
        canary, script = _canary_script(w, hook)
        shutil.copy(script, w.repo / ".git/hooks" / hook)
        canaries.append(canary)
    # a hooksPath pointing at a directory of hooks must be overridden too
    hp = w.tmp / "hostile-hooks"
    hp.mkdir()
    canary, script = _canary_script(w, "hooksPath")
    for hook in ("pre-push", "reference-transaction", "post-merge"):
        shutil.copy(script, hp / hook)
    canaries.append(canary)
    w.host("config", "core.hooksPath", str(hp))

    # control: prove these hooks WOULD run under plain git, so the assertions
    # below can't pass vacuously
    sh("commit", "-q", "--allow-empty", "-m", "control", cwd=w.repo)
    assert canaries[0].exists() or canaries[2].exists() or canary.exists(), "control hook did not fire"
    for c in canaries:
        c.unlink(missing_ok=True)

    g = w.ops()
    # NB: only agent-side calls below. A host `git checkout` here would fire the
    # planted reference-transaction hook itself and fake a failure.
    w.upstream("sub-folder-1/up.txt", "up\n")
    w.write("sub-folder-1/a.txt", "local\n")
    g.commit("c")
    g.pull("merge")
    g.push()
    w.upstream("sub-folder-1/up2.txt", "up2\n")
    w.write("sub-folder-1/b.txt", "local2\n")
    g.commit("c2")
    g.pull("rebase")
    survivors = [c.name for c in canaries if c.exists()]
    assert not survivors, f"hooks ran: {survivors}"


@test
def host_config_that_names_programs_is_overridden() -> None:
    w = World()
    cfgs = {}
    for key in ("core.fsmonitor", "core.pager", "diff.external", "core.editor", "sequence.editor"):
        canary, script = _canary_script(w, key.replace(".", "_"))
        w.host("config", key, script)
        cfgs[key] = canary
    canary, script = _canary_script(w, "sshcmd")
    w.host("config", "core.sshCommand", script)
    cfgs["core.sshCommand"] = canary
    g = w.ops()
    w.write("sub-folder-1/a.txt", "x\n")
    g.status()
    g.diff()
    g.log()
    g.commit("c")
    g.fetch()
    g.push()
    ran = [k for k, c in cfgs.items() if c.exists()]
    assert not ran, f"host-configured programs ran: {ran}"


@test
def gitattributes_naming_undefined_drivers_executes_nothing() -> None:
    w = World()
    g = w.ops()
    w.write("sub-folder-1/.gitattributes", "* filter=pwn diff=pwn merge=pwn\n")
    w.write("sub-folder-1/a.txt", "x\n")
    g.commit("attrs")
    w.upstream("sub-folder-1/up.txt", "up\n")
    w.write("sub-folder-1/a.txt", "y\n")
    g.commit("more")
    assert g.pull("merge")["ok"] and g.push()["ok"]


@test
def startup_refuses_a_config_include_inside_the_working_tree() -> None:
    w = World()
    (w.repo / "sub-folder-1/evil.gitconfig").write_text("[core]\n\tfsmonitor = /bin/true\n")
    with open(w.repo / ".git/config", "a") as f:
        f.write("[include]\n\tpath = ../sub-folder-1/evil.gitconfig\n")
    try:
        w.ops()
    except RuntimeError as exc:
        assert "inside the working tree" in str(exc), exc
    else:
        raise AssertionError("an include of an agent-writable file was accepted")
    # An include of a file OUTSIDE the tree (host-controlled) is the operator's business.
    w2 = World()
    (w2.tmp / "team.gitconfig").write_text("[core]\n\tabbrev = 9\n")
    with open(w2.repo / ".git/config", "a") as f:
        f.write(f"[include]\n\tpath = {w2.tmp}/team.gitconfig\n")
    w2.ops()


@test
def startup_refusals() -> None:
    def refused(world: World, needle: str, **kw: Any) -> None:
        try:
            world.ops(**kw)
        except RuntimeError as exc:
            assert needle in str(exc), f"wanted {needle!r} in: {exc}"
        else:
            raise AssertionError(f"startup accepted a bad deployment (wanted {needle!r})")

    w = World()
    refused(w, "DENIED_NAMES", denied_names=frozenset({".gitmodules"}))
    keyfile = w.tmp / "key"
    keyfile.write_text("not a real key")
    refused(w, "SSH_KNOWN_HOSTS_PATH is required", ssh_key_path=str(keyfile))
    (w.tmp / "kdir").mkdir()
    refused(w, "is a directory", ssh_key_path=str(w.tmp / "kdir"), ssh_known_hosts_path=str(keyfile))
    refused(w, "does not match GIT_BRANCH", git_branch="-bad")   # refused before any name check is needed
    refused(w, "does not match GIT_BRANCH", git_branch="other")
    refused(w, "not configured", git_remote="nope")
    refused(w, "must be GIT_REPO_ROOT", root=str(w.tmp))                      # DATA_ROOT outside the repo

    w.host("checkout", "-q", "--detach")
    refused(w, "detached")
    w.host("checkout", "-q", "main")

    # a symlinked path between the repo root and DATA_ROOT
    (w.repo / "real").mkdir()
    (w.repo / "real/inner").mkdir()
    os.symlink("real", w.repo / "link")
    refused(w, "symlink", root=str(w.repo / "link" / "inner"))

    # DATA_ROOT inside .git
    refused(w, "inside .git", root=str(w.repo / ".git" / "refs"))

    # .git as a gitlink file
    w3 = World()
    shutil.move(str(w3.repo / ".git"), str(w3.tmp / "gitdir"))
    (w3.repo / ".git").write_text(f"gitdir: {w3.tmp / 'gitdir'}\n")
    refused(w3, "real directory")

    # no commits
    w4 = World()
    empty = w4.tmp / "empty"
    empty.mkdir()
    sh("init", "-q", "-b", "main", cwd=empty)
    (empty / "d").mkdir()
    cfg_world = World()
    cfg_world.repo = empty
    cfg_world.scope_dir = "d"
    refused(cfg_world, "no commits")


@test
def only_ssh_https_and_scp_style_remotes_are_accepted() -> None:
    saved_patterns, saved_protocols = git_ops.EXTRA_URL_PATTERNS[:], git_ops.EXTRA_PROTOCOLS[:]
    git_ops.EXTRA_URL_PATTERNS[:] = []
    git_ops.EXTRA_PROTOCOLS[:] = []
    try:
        w = World()
        for bad in (str(w.origin), f"file://{w.origin}", "ext::sh -c 'touch /tmp/x'", "git://host/repo",
                    "http://host/repo", "-oProxyCommand=x", "ftp://host/x", "/abs/path"):
            w.host("config", "--", "remote.origin.url", bad)
            try:
                w.ops()
            except RuntimeError as exc:
                assert "URL scheme" in str(exc) or "git" in str(exc), exc
            else:
                raise AssertionError(f"remote URL {bad!r} was accepted")
        for good in ("ssh://git@host/repo.git", "git@github.com:org/repo.git", "https://host/org/repo.git"):
            assert git_ops.GitRepo._url_allowed(good), good
        for bad in ("-oProxyCommand=touch@host:x", "-oFoo@host:x", "git@-oProxyCommand=x:y",
                    "ssh://-oProxyCommand=x/y", "https://-evil/x", "ssh://", "git@host", "x@y:\nz"):
            assert not git_ops.GitRepo._url_allowed(bad), f"{bad!r} must be refused"
        # read-only mode never touches the network, so it does not inspect the remote
        w.host("config", "remote.origin.url", str(w.origin))
        w.ops(enable_git_write=False)
    finally:
        git_ops.EXTRA_URL_PATTERNS[:] = saved_patterns
        git_ops.EXTRA_PROTOCOLS[:] = saved_protocols


@test
def credentials_never_reach_output_or_argv() -> None:
    w = World()
    token = "ghp_CANARYTOKEN0123456789abcdef"
    w.host("remote", "set-url", "origin", f"https://x-access-token:{token}@127.0.0.1:9/org/repo.git")
    g = w.ops(git_token=token, git_network_timeout_seconds=15)
    argv = " ".join(g._c_flags)
    assert token not in argv, "the token must not appear in git's argv (ps would show it)"
    assert "x-access-token" not in argv.replace("username=x-access-token", "")
    w.write("sub-folder-1/a.txt", "x\n")
    g.commit("c")
    for fn in (g.fetch, g.push, g.pull):
        try:
            fn()
        except GitOpError as exc:
            blob = exc.message + str(exc.detail)
            assert token not in blob and "x-access-token" not in blob, f"credential leaked: {blob}"
            assert exc.code in ("network", "auth_failed"), f"{exc.code}: {exc.message}"
        else:
            raise AssertionError("expected a connection failure")
    # and the scrubber itself
    dirty = f"fatal: unable to access 'https://user:{token}@host/x': {token}\n{g._state_dir}/token"
    clean = g._scrub(dirty)
    assert token not in clean and "user:" not in clean and g._state_dir not in clean


@test
def a_stalled_remote_times_out_without_leaving_locks_or_processes() -> None:
    w = World()
    marker = f"sleep {61 + os.getpid() % 7}"
    w.host("config", "remote.origin.uploadpack", f"{marker} #")
    g = w.ops(git_network_timeout_seconds=2)
    w.write("sub-folder-1/a.txt", "x\n")
    g.commit("c")
    t0 = time.monotonic()
    expect_err("timeout", g.fetch)
    assert time.monotonic() - t0 < 15, "timeout was not enforced"
    time.sleep(0.3)
    # (/proc scan rather than pgrep: the runtime image has no procps)
    left = [
        d for d in os.listdir("/proc") if d.isdigit() and int(d) != os.getpid()
        and marker.encode() in _read_bytes(f"/proc/{d}/cmdline")
    ]
    assert not left, f"orphaned child processes survived the timeout: {left}"
    assert not (w.repo / ".git/index.lock").exists()
    w.write("sub-folder-1/b.txt", "y\n")
    assert g.commit("after the timeout")["ok"], "a killed fetch must not wedge the next commit"


@test
def output_cap_stops_git_instead_of_buffering_it() -> None:
    w = World()
    g = w.ops()
    w.write("sub-folder-1/a.txt", "".join(f"line {i} " + "x" * 200 + "\n" for i in range(4000)))
    saved = git_ops.MAX_OUTPUT_BYTES
    git_ops.MAX_OUTPUT_BYTES = 50_000
    try:
        exc = expect_err("git_failed", g.diff, inline_limit=10**9)
        assert "more than" in exc.message and "stopped" in exc.message, exc.message
    finally:
        git_ops.MAX_OUTPUT_BYTES = saved
    assert len(g.diff(inline_limit=10**9)["diff"]) > 50_000, "the cap must only apply when exceeded"
    # a process killed for its output must not wedge the next call
    w.write("sub-folder-1/b.txt", "y\n")
    assert g.commit("after the cap")["ok"]


@test
def locked_index_is_reported_and_never_deleted() -> None:
    w = World()
    g = w.ops()
    w.write("sub-folder-1/a.txt", "x\n")
    lock = w.repo / ".git/index.lock"
    lock.write_text("held by a person on the host")
    t0 = time.monotonic()
    try:
        # the real-index refresh after update-ref is best-effort, but the
        # temp-index commit itself needs no real-index lock; what must hold is
        # that the lock file is left exactly as found
        g.commit("c")
    except GitOpError as exc:
        assert exc.code == "locked", exc
    assert lock.read_text() == "held by a person on the host", "the lock file was touched"
    lock.unlink()
    assert time.monotonic() - t0 < 30


@test
def source_only_invokes_an_allow_list_of_git_subcommands() -> None:
    """I7 as a test: every git subcommand the module can run is named here, so
    adding reset/clean/checkout/branch -D/push --force means changing this
    test too, in the same diff, where a reviewer will see it."""
    src = (ROOT / "app/git_ops.py").read_text()
    code = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))
    allowed = {
        "rev-parse", "symbolic-ref", "check-ref-format", "remote", "config", "status", "log",
        "diff", "diff-tree", "diff-index", "rev-list", "merge-base", "merge-tree", "read-tree",
        "add", "ls-tree", "write-tree", "commit-tree", "update-ref", "fetch", "push", "merge", "rebase",
    }
    used = set(re.findall(r'self\._(?:git|out)\(\s*\[\s*"([a-z-]+)"', code))
    used |= set(re.findall(r'abort = \["([a-z-]+)", "--abort"\]', code))
    used |= set(re.findall(r'else \["([a-z-]+)", "--abort"\]', code))
    assert used, "the regex found no git calls; this test needs updating"
    unexpected = used - allowed
    assert not unexpected, f"git_ops.py now runs subcommands this test does not allow: {sorted(unexpected)}"
    assert {"push", "merge", "rebase", "commit-tree"} <= used, f"regex is not seeing the real calls: {sorted(used)}"
    # flags that destroy or force, anywhere in any string literal
    for flag in ("--force", "--force-with-lease", "--delete", "--hard", "--mirror", "--prune", "-D", "-f"):
        assert f'"{flag}"' not in code, f"destructive flag {flag} present"
    for bad in ("shell=True", "os.system", "--amend", "--allow-empty", "--no-verify", "--autostash"):
        assert bad not in code.replace('"--no-autostash"', ""), f"{bad} present"
    # the only '+' refspec is the fetch of the remote-tracking ref; push has none
    plus = [ln.strip() for ln in code.splitlines() if "+refs" in ln]
    assert len(plus) == 1 and "refs/heads/" in plus[0] and "{ref}" in plus[0], plus
    for call in re.findall(r'\["push"[^\]]*\]', code):
        assert "+" not in call and "force" not in call, call


@test
def writes_during_a_git_operation_wait_or_fail_busy() -> None:
    w = World()
    g = w.ops()
    w.write("sub-folder-1/a.txt", "x\n")
    seen: dict[str, Any] = {}

    def writer() -> None:
        try:
            g.vault.mkdir(["newdir"], parents=False)
            seen["r"] = "ok"
        except PathError as exc:
            seen["r"] = (exc.status, exc.code)

    with w.gate.exclusive():
        t = threading.Thread(target=writer)
        t.start()
        t.join()
    assert seen["r"] == (423, "git_busy"), seen
    t = threading.Thread(target=writer)
    t.start()
    t.join()
    assert seen["r"] == "ok"


# ----------------------------------------------------------------------------
# runner
# ----------------------------------------------------------------------------


def seam_on() -> None:
    git_ops.EXTRA_URL_PATTERNS[:] = [r"^/"]
    git_ops.EXTRA_PROTOCOLS[:] = ["file"]


def run_in_process(only: str | None = None) -> int:
    seam_on()
    # globals() preserves definition order, which is the order read top-to-bottom.
    tests = [
        (n, f) for n, f in list(globals().items())
        if getattr(f, "_is_test", False) and (only is None or only in n)
    ]
    for name, fn in tests:
        try:
            fn()
        except Exception:  # noqa: BLE001
            FAILED.append((name, traceback.format_exc()))
            print(f"  FAIL  {name}")
        else:
            PASSED.append(name)
            print(f"  PASS  {name}")
    for d in _TMPDIRS:
        shutil.rmtree(d, ignore_errors=True)
    print()
    for name, tb in FAILED:
        print(f"--- {name}\n{tb}")
    print(f"{len(PASSED)} passed, {len(FAILED)} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(run_in_process(sys.argv[1] if len(sys.argv) > 1 else None))
