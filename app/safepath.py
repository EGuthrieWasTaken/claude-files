"""Filesystem access confined to a root directory.

Containment strategy, strongest to weakest:

1.  The container only bind-mounts the target subfolder, so the rest of the
    repository is not present in the mount namespace at all.
2.  Every path is walked one component at a time using ``openat`` against a
    directory file descriptor pinned at startup, with ``O_NOFOLLOW`` on each
    component.  A symlink anywhere in the path fails with ELOOP rather than
    being followed, and because each step is an fd (not a string) there is no
    time-of-check/time-of-use window for a symlink swap.
3.  Path components are validated syntactically before any syscall: no "..",
    no empty components, no absolute paths, no NUL bytes, no backslashes.

Nothing in this module ever builds a full path string and hands it to the OS,
which is what makes traversal structurally impossible rather than filtered.
"""

from __future__ import annotations

import errno
import os
import stat
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from typing import Iterator

# Errnos that mean "you tried to walk through a symlink".  Linux reports
# ELOOP; some BSD-derived kernels report EMLINK for O_NOFOLLOW.
_SYMLINK_ERRNOS = {errno.ELOOP, errno.EMLINK}

_O_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_O_FILE_READ = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC

MAX_COMPONENT_LEN = 255
MAX_DEPTH = 32


class PathError(Exception):
    """A path was rejected. Carries the HTTP status the API should return."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class Denied(PathError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(403, code, message)


class NotFound(PathError):
    def __init__(self, message: str = "No such file or directory") -> None:
        super().__init__(404, "not_found", message)


class NoParent(PathError):
    """A write named a path whose parent directory does not exist.

    Distinct from NotFound so the caller can tell "the file isn't there" from
    "the folder you're writing into isn't there" -- a 404 on a PUT reads as
    the former and sends clients chasing the wrong problem.
    """

    def __init__(self, parent: str, missing: str) -> None:
        super().__init__(
            409,
            "no_parent",
            f"Parent directory '{parent}' does not exist"
            + (f" (missing at '{missing}')" if missing != parent else "")
            + ". Retry with parents enabled (`?parents=true` on a write, or "
            '`"parents": true` in a /v1/move body), create it explicitly with '
            f"POST /v1/mkdir/{parent}, or call GET /v1/list to see what exists.",
        )
        self.parent = parent
        self.missing = missing


class Conflict(PathError):
    def __init__(self, message: str) -> None:
        super().__init__(409, "conflict", message)


@dataclass(frozen=True)
class Entry:
    name: str
    path: str
    type: str          # "file" | "dir" | "symlink" | "other"
    size: int | None
    mtime: float | None
    readable: bool     # False for anything we refuse to serve


def split_components(raw: str, *, denied_names: frozenset[str]) -> list[str]:
    """Validate a client-supplied relative path and split it into components.

    Rejects anything that could denote a location outside the root, before a
    single syscall is issued.  Returns [] for the root itself.
    """
    if raw is None:
        return []
    if "\x00" in raw:
        raise Denied("invalid_path", "Path contains a NUL byte")
    if "\\" in raw:
        raise Denied("invalid_path", "Backslashes are not allowed in paths")
    if raw.startswith("/"):
        raise Denied("absolute_path", "Absolute paths are not allowed")

    trimmed = raw.strip("/")
    if trimmed in ("", "."):
        return []

    parts = trimmed.split("/")
    if len(parts) > MAX_DEPTH:
        raise Denied("too_deep", f"Path exceeds maximum depth of {MAX_DEPTH}")

    for part in parts:
        if part == "":
            raise Denied("invalid_path", "Path contains an empty component")
        if part == ".":
            raise Denied("invalid_path", "Path contains a '.' component")
        if part == "..":
            raise Denied("traversal", "Path traversal ('..') is not allowed")
        if len(part.encode("utf-8")) > MAX_COMPONENT_LEN:
            raise Denied("name_too_long", "Path component is too long")
        if part.lower() in denied_names:
            raise Denied("denied_name", f"Access to '{part}' is not allowed")

    return parts


def join_components(parts: list[str]) -> str:
    return "/".join(parts)


def _translate_oserror(exc: OSError, what: str) -> PathError:
    if exc.errno in _SYMLINK_ERRNOS:
        return Denied("symlink", f"Refusing to follow a symlink in {what}")
    if exc.errno == errno.ENOENT:
        return NotFound()
    if exc.errno == errno.ENOTDIR:
        return Conflict("A path component is not a directory")
    if exc.errno == errno.EACCES or exc.errno == errno.EPERM:
        return Denied("permission", "Permission denied")
    if exc.errno == errno.ENAMETOOLONG:
        return Denied("name_too_long", "Path component is too long")
    if exc.errno == errno.ENOSPC:
        return PathError(507, "no_space", "No space left on device")
    if exc.errno == errno.EDQUOT:
        return PathError(507, "quota", "Storage quota exceeded")
    return PathError(500, "io_error", f"I/O error: {exc.strerror}")


class Vault:
    """All filesystem access for one root directory."""

    def __init__(
        self,
        root: str,
        *,
        denied_names: frozenset[str] = frozenset({".git"}),
        allow_hardlinked_files: bool = False,
    ) -> None:
        self.root_path = os.path.realpath(root)
        self.denied_names = denied_names
        self.allow_hardlinked_files = allow_hardlinked_files

        if not os.path.isdir(self.root_path):
            raise RuntimeError(f"Root directory does not exist: {self.root_path}")
        # Pinned for the lifetime of the process.  Even if the path is
        # re-pointed underneath us later, this fd still refers to the
        # directory we validated at startup.
        self._root_fd = os.open(self.root_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        st = os.fstat(self._root_fd)
        self.root_dev = st.st_dev

    def sweep_temp_files(self) -> int:
        """Remove leftover .upload-*.tmp files from an interrupted upload."""
        removed = 0
        queue: list[list[str]] = [[]]
        while queue:
            parts = queue.pop(0)
            try:
                with self._dir_fd(parts) as fd:
                    with os.scandir(fd) as it:
                        entries = [(d.name, d.is_dir(follow_symlinks=False)) for d in it]
                    for name, is_dir in entries:
                        if is_dir:
                            queue.append(parts + [name])
                        elif name.startswith(".upload-") and name.endswith(".tmp"):
                            try:
                                os.unlink(name, dir_fd=fd)
                                removed += 1
                            except OSError:
                                pass
            except PathError:
                continue
        return removed

    def close(self) -> None:
        try:
            os.close(self._root_fd)
        except OSError:
            pass

    # -- path resolution --------------------------------------------------

    def components(self, raw: str) -> list[str]:
        return split_components(raw, denied_names=self.denied_names)

    @contextmanager
    def _dir_fd(self, parts: list[str]) -> Iterator[int]:
        """Open the directory named by ``parts``, walking one component at a
        time with O_NOFOLLOW.  Yields an fd owned by this context manager."""
        fd = os.dup(self._root_fd)
        try:
            for depth, part in enumerate(parts):
                try:
                    nxt = os.open(part, _O_DIR, dir_fd=fd)
                except OSError as exc:
                    where = join_components(parts[: depth + 1])
                    # O_NOFOLLOW|O_DIRECTORY reports a symlinked component as
                    # ELOOP on some kernels and ENOTDIR on others. Look at the
                    # entry itself so the caller gets an accurate reason.
                    if exc.errno == errno.ENOTDIR:
                        try:
                            st = os.stat(part, dir_fd=fd, follow_symlinks=False)
                            if stat.S_ISLNK(st.st_mode):
                                raise Denied(
                                    "symlink", f"Refusing to follow a symlink in '{where}'"
                                ) from exc
                        except OSError:
                            pass
                    raise _translate_oserror(exc, f"'{where}'") from exc
                os.close(fd)
                fd = nxt
                self._assert_same_device(fd, join_components(parts[: depth + 1]))
            yield fd
        finally:
            try:
                os.close(fd)
            except OSError:
                pass

    @contextmanager
    def _parent_fd(
        self,
        parts: list[str],
        *,
        for_write: bool = False,
        create_parents: bool = False,
    ) -> Iterator[tuple[int, str]]:
        """Yield (fd of the parent directory, final component name).

        ``for_write`` reports a missing parent as NoParent rather than a bare
        404. Reads keep 404, which is the conventional answer there.
        ``create_parents`` makes the parent chain first, like ``mkdir -p``.
        """
        if not parts:
            raise Denied("is_root", "This operation cannot target the root directory")

        if create_parents and len(parts) > 1:
            self.mkdir(parts[:-1], parents=True)

        with ExitStack() as stack:
            try:
                fd = stack.enter_context(self._dir_fd(parts[:-1]))
            except NotFound as exc:
                if for_write:
                    raise self._no_parent_error(parts) from exc
                raise
            yield fd, parts[-1]

    def _no_parent_error(self, parts: list[str]) -> NoParent:
        """Identify which ancestor is actually missing, for the error message."""
        parent = join_components(parts[:-1])
        missing = parent
        for i in range(1, len(parts)):
            try:
                with self._dir_fd(parts[:i]):
                    continue
            except PathError:
                missing = join_components(parts[:i])
                break
        return NoParent(parent, missing)

    def _assert_same_device(self, fd: int, where: str) -> None:
        """Refuse to descend into a different filesystem.

        A nested bind mount or an overlay planted inside the shared folder
        would otherwise widen the reachable surface beyond what was mounted.
        """
        if os.fstat(fd).st_dev != self.root_dev:
            raise Denied("crosses_mount", f"'{where}' is on a different filesystem")

    # -- metadata ---------------------------------------------------------

    def _classify(self, st: os.stat_result) -> tuple[str, bool]:
        if stat.S_ISLNK(st.st_mode):
            return "symlink", False
        if stat.S_ISDIR(st.st_mode):
            return "dir", False
        if stat.S_ISREG(st.st_mode):
            if st.st_nlink > 1 and not self.allow_hardlinked_files:
                # A hard link created on the host can alias a file outside the
                # mounted subtree.  Nothing inside the container can create
                # one, but refusing them closes the vector entirely.
                return "file", False
            return "file", True
        return "other", False

    def stat(self, parts: list[str]) -> Entry:
        if not parts:
            with self._dir_fd(parts) as fd:
                st = os.fstat(fd)
            return Entry(name="", path="", type="dir", size=None, mtime=st.st_mtime, readable=False)

        with self._parent_fd(parts) as (fd, name):
            try:
                st = os.stat(name, dir_fd=fd, follow_symlinks=False)
            except OSError as exc:
                raise _translate_oserror(exc, f"'{join_components(parts)}'") from exc

        kind, readable = self._classify(st)
        return Entry(
            name=name,
            path=join_components(parts),
            type=kind,
            size=st.st_size if kind == "file" else None,
            mtime=st.st_mtime,
            readable=readable,
        )

    def listdir(self, parts: list[str]) -> list[Entry]:
        prefix = join_components(parts)
        out: list[Entry] = []
        with self._dir_fd(parts) as fd:
            # scandir(fd) does not follow symlinks to build the listing; each
            # entry is stat'ed with follow_symlinks=False below.
            with os.scandir(fd) as it:
                for de in it:
                    if de.name.lower() in self.denied_names:
                        continue
                    try:
                        st = de.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    kind, readable = self._classify(st)
                    out.append(
                        Entry(
                            name=de.name,
                            path=f"{prefix}/{de.name}" if prefix else de.name,
                            type=kind,
                            size=st.st_size if kind == "file" else None,
                            mtime=st.st_mtime,
                            readable=readable,
                        )
                    )
        out.sort(key=lambda e: (e.type != "dir", e.name))
        return out

    def walk(self, parts: list[str], *, max_entries: int) -> list[Entry]:
        """Recursive listing, breadth-first, never crossing a symlink."""
        out: list[Entry] = []
        queue: list[list[str]] = [parts]
        while queue:
            current = queue.pop(0)
            for entry in self.listdir(current):
                out.append(entry)
                if len(out) >= max_entries:
                    return out
                if entry.type == "dir":
                    queue.append(current + [entry.name])
        return out

    # -- read -------------------------------------------------------------

    def open_read(self, parts: list[str]) -> tuple[int, os.stat_result]:
        """Open a regular file for reading. Caller owns the returned fd."""
        with self._parent_fd(parts) as (dir_fd, name):
            try:
                fd = os.open(name, _O_FILE_READ, dir_fd=dir_fd)
            except OSError as exc:
                raise _translate_oserror(exc, f"'{join_components(parts)}'") from exc

        try:
            st = os.fstat(fd)
            if stat.S_ISDIR(st.st_mode):
                raise Conflict("Path is a directory, not a file")
            if not stat.S_ISREG(st.st_mode):
                raise Denied("not_regular", "Path is not a regular file")
            if st.st_nlink > 1 and not self.allow_hardlinked_files:
                raise Denied(
                    "hardlink",
                    "Refusing to read a file with multiple hard links; it may alias "
                    "a file outside the shared folder",
                )
            if st.st_dev != self.root_dev:
                raise Denied("crosses_mount", "File is on a different filesystem")
        except Exception:
            os.close(fd)
            raise
        return fd, st

    # -- write ------------------------------------------------------------

    @contextmanager
    def atomic_write(
        self, parts: list[str], *, overwrite: bool, create_parents: bool = False
    ) -> Iterator[tuple[int, "Committer"]]:
        """Create a temp file next to the target and rename it into place.

        Yields (write fd, committer).  If the body of the ``with`` raises, the
        temp file is removed and the target is left untouched.
        """
        with self._parent_fd(
            parts, for_write=True, create_parents=create_parents
        ) as (dir_fd, name):
            if not overwrite:
                try:
                    os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
                except OSError as exc:
                    if exc.errno != errno.ENOENT:
                        raise _translate_oserror(exc, f"'{join_components(parts)}'") from exc
                else:
                    raise Conflict("File already exists")
            else:
                # Refuse to write *through* an existing symlink or onto a dir.
                try:
                    st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
                except OSError as exc:
                    if exc.errno != errno.ENOENT:
                        raise _translate_oserror(exc, f"'{join_components(parts)}'") from exc
                else:
                    if stat.S_ISDIR(st.st_mode):
                        raise Conflict("Path is a directory, not a file")
                    if stat.S_ISLNK(st.st_mode):
                        raise Denied("symlink", "Refusing to overwrite a symlink")

            tmp_name = f".upload-{os.urandom(8).hex()}.tmp"
            try:
                fd = os.open(
                    tmp_name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                    0o644,
                    dir_fd=dir_fd,
                )
            except OSError as exc:
                raise _translate_oserror(exc, "the target directory") from exc

            committer = Committer(dir_fd=dir_fd, tmp_name=tmp_name, final_name=name)
            try:
                yield fd, committer
            finally:
                try:
                    os.close(fd)
                except OSError:
                    pass
                committer.cleanup()

    def mkdir(self, parts: list[str], *, parents: bool) -> bool:
        """Create a directory. Returns True if it was created, False if it existed."""
        if not parts:
            return False
        if parents:
            created = False
            for i in range(1, len(parts) + 1):
                created = self._mkdir_one(parts[:i]) or created
            return created
        return self._mkdir_one(parts)

    def _mkdir_one(self, parts: list[str]) -> bool:
        with self._parent_fd(parts) as (dir_fd, name):
            try:
                os.mkdir(name, 0o755, dir_fd=dir_fd)
                return True
            except FileExistsError:
                st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
                if stat.S_ISLNK(st.st_mode):
                    raise Denied("symlink", "Path exists and is a symlink") from None
                if not stat.S_ISDIR(st.st_mode):
                    raise Conflict("Path exists and is not a directory") from None
                return False
            except OSError as exc:
                raise _translate_oserror(exc, f"'{join_components(parts)}'") from exc

    def delete(self, parts: list[str], *, recursive: bool) -> None:
        with self._parent_fd(parts) as (dir_fd, name):
            try:
                st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
            except OSError as exc:
                raise _translate_oserror(exc, f"'{join_components(parts)}'") from exc

            if stat.S_ISDIR(st.st_mode):
                if recursive:
                    self._rmtree(dir_fd, name, join_components(parts))
                else:
                    try:
                        os.rmdir(name, dir_fd=dir_fd)
                    except OSError as exc:
                        if exc.errno in (errno.ENOTEMPTY, errno.EEXIST):
                            raise Conflict(
                                "Directory is not empty; pass recursive=true"
                            ) from exc
                        raise _translate_oserror(exc, f"'{join_components(parts)}'") from exc
            else:
                # unlink never follows symlinks: this removes the link itself,
                # never its target.
                try:
                    os.unlink(name, dir_fd=dir_fd)
                except OSError as exc:
                    raise _translate_oserror(exc, f"'{join_components(parts)}'") from exc

    def _rmtree(self, parent_fd: int, name: str, where: str) -> None:
        try:
            fd = os.open(name, _O_DIR, dir_fd=parent_fd)
        except OSError as exc:
            raise _translate_oserror(exc, f"'{where}'") from exc
        try:
            if os.fstat(fd).st_dev != self.root_dev:
                raise Denied("crosses_mount", f"'{where}' is on a different filesystem")
            with os.scandir(fd) as it:
                entries = list(it)
            for de in entries:
                if de.is_dir(follow_symlinks=False):
                    self._rmtree(fd, de.name, f"{where}/{de.name}")
                else:
                    os.unlink(de.name, dir_fd=fd)
        finally:
            os.close(fd)
        try:
            os.rmdir(name, dir_fd=parent_fd)
        except OSError as exc:
            raise _translate_oserror(exc, f"'{where}'") from exc

    def move(
        self, src: list[str], dst: list[str], *, overwrite: bool, create_parents: bool = False
    ) -> None:
        if not src or not dst:
            raise Denied("is_root", "Cannot move the root directory")
        with self._parent_fd(src) as (src_fd, src_name):
            with self._parent_fd(
                dst, for_write=True, create_parents=create_parents
            ) as (dst_fd, dst_name):
                try:
                    os.stat(src_name, dir_fd=src_fd, follow_symlinks=False)
                except OSError as exc:
                    raise _translate_oserror(exc, f"'{join_components(src)}'") from exc
                if not overwrite:
                    try:
                        os.stat(dst_name, dir_fd=dst_fd, follow_symlinks=False)
                    except OSError as exc:
                        if exc.errno != errno.ENOENT:
                            raise _translate_oserror(exc, f"'{join_components(dst)}'") from exc
                    else:
                        raise Conflict("Destination already exists")
                try:
                    os.rename(src_name, dst_name, src_dir_fd=src_fd, dst_dir_fd=dst_fd)
                except OSError as exc:
                    raise _translate_oserror(exc, "the rename") from exc


class Committer:
    """Renames the temp file into place, or cleans it up."""

    def __init__(self, *, dir_fd: int, tmp_name: str, final_name: str) -> None:
        self._dir_fd = dir_fd
        self._tmp_name = tmp_name
        self._final_name = final_name
        self._done = False

    def commit(self, fd: int) -> None:
        os.fsync(fd)
        try:
            # rename() replaces the destination entry itself.  If the
            # destination is a symlink, the link is replaced -- the target is
            # never written through.
            os.rename(
                self._tmp_name,
                self._final_name,
                src_dir_fd=self._dir_fd,
                dst_dir_fd=self._dir_fd,
            )
        except OSError as exc:
            raise _translate_oserror(exc, "the commit") from exc
        self._done = True
        try:
            os.fsync(self._dir_fd)
        except OSError:
            pass

    def cleanup(self) -> None:
        if self._done:
            return
        try:
            os.unlink(self._tmp_name, dir_fd=self._dir_fd)
        except OSError:
            pass
        self._done = True
