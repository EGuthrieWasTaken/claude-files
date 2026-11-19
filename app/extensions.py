"""Extension modules: extra MCP tools loaded at startup from EXTRA_TOOL_MODULES.

Design brief: docs/extensions.md. In short, a derived image (``FROM`` a pinned
claude-files tag) adds a Python package and names it in EXTRA_TOOL_MODULES;
the tools it registers appear on the same /mcp endpoint, behind the same token
check, writing through the same ``Vault`` and worktree gate as ``write_file``.
Nothing in this file knows what any extension does.

The contract an extension module implements:

* ``register(api: ExtensionAPI) -> None`` -- required. Registers tools with
  ``api.add_tool``. Called once, at startup, after the core tools.
* ``INSTRUCTIONS: str`` -- optional. Appended to the MCP server instructions.
* ``REQUIRES_API: int`` -- optional, default 1. The server refuses to start if
  it is greater than ``EXTENSION_API_VERSION``.

Every failure here -- a bad name, a module that won't import, a missing
``register``, a duplicate tool name, an exception inside ``register`` -- is an
``ExtensionError`` that stops the server from starting. ``ExtensionError`` is a
``RuntimeError`` and deliberately NOT an ``ImportError``: app/main.py treats an
``ImportError`` from ``mcp_endpoint`` as "mcp not installed" and quietly runs
without /mcp, which is the wrong outcome for an extension that failed to load.
"""

from __future__ import annotations

import functools
import importlib
import inspect
import logging
import re
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any, Callable

log = logging.getLogger("claude-files.extensions")

# Bump when ExtensionAPI changes in a way an existing extension could notice.
# Adding a new attribute or method is not such a change; removing or changing
# one is.
EXTENSION_API_VERSION = 1

_MODULE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")


class ExtensionError(RuntimeError):
    """An extension module could not be loaded or registered."""


@dataclass
class LoadedExtension:
    name: str
    module: ModuleType
    instructions: str
    tools: list[str] = field(default_factory=list)


def parse_module_list(raw: str) -> tuple[str, ...]:
    """EXTRA_TOOL_MODULES: comma-separated dotted module names, order kept."""
    names = [n.strip() for n in raw.split(",") if n.strip()]
    for n in names:
        if not _MODULE_NAME.match(n):
            raise ExtensionError(
                f"EXTRA_TOOL_MODULES entry {n!r} is not a dotted Python module name "
                "(e.g. 'claude_files_org' or 'pkg.tools')"
            )
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        raise ExtensionError(f"EXTRA_TOOL_MODULES names {', '.join(dupes)} more than once")
    return tuple(names)


def load(names: tuple[str, ...]) -> list[LoadedExtension]:
    """Import each module and check it implements the contract. Registers
    nothing: that waits until the core tools exist (``register_all``)."""
    loaded: list[LoadedExtension] = []
    for name in names:
        try:
            mod = importlib.import_module(name)
        except Exception as exc:  # ImportError included -- see module docstring
            raise ExtensionError(
                f"extension module {name!r} (EXTRA_TOOL_MODULES) failed to import: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

        requires = getattr(mod, "REQUIRES_API", 1)
        if not isinstance(requires, int) or isinstance(requires, bool):
            raise ExtensionError(f"extension {name!r}: REQUIRES_API must be an int, got {requires!r}")
        if requires > EXTENSION_API_VERSION:
            raise ExtensionError(
                f"extension {name!r} needs extension API {requires}, but this claude-files "
                f"provides {EXTENSION_API_VERSION}: build it on a newer claude-files image"
            )

        register = getattr(mod, "register", None)
        if not callable(register):
            raise ExtensionError(f"extension {name!r} has no register(api) function")

        instructions = getattr(mod, "INSTRUCTIONS", "")
        if not isinstance(instructions, str):
            raise ExtensionError(f"extension {name!r}: INSTRUCTIONS must be a str")

        loaded.append(LoadedExtension(name=name, module=mod, instructions=instructions.strip()))
    return loaded


def combined_instructions(base: str, loaded: list[LoadedExtension]) -> str:
    parts = [base.rstrip("\n")]
    parts += [ext.instructions for ext in loaded if ext.instructions]
    return "\n\n".join(parts) + "\n"


class ExtensionAPI:
    """What an extension's ``register()`` receives. Deliberately narrow: an
    extension gets the shared Vault (all file access goes through it, so path
    containment, denied names, symlink refusal and the worktree gate apply
    unchanged), the config, the git repo when git mode is on, and a way to add
    tools -- not the FastMCP object or the FastAPI app."""

    api_version = EXTENSION_API_VERSION

    def __init__(
        self,
        ext: LoadedExtension,
        *,
        mcp: Any,
        config: Any,
        vault: Any,
        git: Any,
        guard: Callable[..., Any],
        require_write: Callable[[Any], None],
    ) -> None:
        self._ext = ext
        self._mcp = mcp
        self._guard = guard
        self._require_write = require_write
        self.name = ext.name
        self.config = config
        self.vault = vault
        self.git = git  # app.git_ops.GitRepo, or None when git mode is off
        self.log = logging.getLogger(f"claude-files.ext.{ext.name}")

    # -- tools ---------------------------------------------------------------

    def add_tool(
        self,
        fn: Callable[..., Any],
        *,
        read_only: bool,
        name: str | None = None,
        title: str | None = None,
        description: str | None = None,
        destructive: bool = False,
        open_world: bool = False,
    ) -> None:
        """Register ``fn`` as an MCP tool.

        ``read_only`` is required, not defaulted, so every tool states it.
        ``read_only=False`` makes the server refuse the call for a read-only
        token before ``fn`` runs -- an extension cannot forget the check. The
        tool's description is ``fn``'s docstring unless ``description`` is
        given. A name already taken (by a core tool or another extension)
        stops the server from starting rather than silently keeping the first.
        """
        from mcp.types import ToolAnnotations

        tool_name = name or fn.__name__
        if self._mcp._tool_manager.get_tool(tool_name) is not None:
            raise ExtensionError(
                f"extension {self.name!r} registers tool {tool_name!r}, but that name "
                "is already taken by a core tool or an earlier extension"
            )
        annotations = ToolAnnotations(
            readOnlyHint=read_only,
            destructiveHint=None if read_only else destructive,
            openWorldHint=open_world,
        )
        target = fn if read_only else self._write_gated(fn)
        self._mcp.add_tool(
            target, name=tool_name, title=title, description=description, annotations=annotations
        )
        self._ext.tools.append(tool_name)

    def _write_gated(self, fn: Callable[..., Any]) -> Callable[..., Any]:
        # functools.wraps sets __wrapped__, which inspect.signature follows, so
        # FastMCP still sees fn's real parameters (including a Context one).
        mcp = self._mcp
        require_write = self._require_write
        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def gated_async(*args: Any, **kwargs: Any) -> Any:
                require_write(mcp.get_context())
                return await fn(*args, **kwargs)

            return gated_async

        @functools.wraps(fn)
        def gated(*args: Any, **kwargs: Any) -> Any:
            require_write(mcp.get_context())
            return fn(*args, **kwargs)

        return gated

    # -- helpers -------------------------------------------------------------

    def guard(self, op: str, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Run ``fn`` the way the core tools do: a PathError, GitOpError or
        ValueError is logged as ``denied MCP <op>`` and reaches the agent as a
        tool error with its message. Use it around every Vault call."""
        return self._guard(op, fn, *args, **kwargs)

    async def run_in_thread(self, op: str, fn: Callable[..., Any]) -> Any:
        """``guard`` in a worker thread, for an ``async def`` tool doing slow
        work, so the server keeps answering other requests meanwhile."""
        import asyncio

        return await asyncio.to_thread(self._guard, op, fn)

    def require_write(self, ctx: Any) -> None:
        """Refuse a read-only token. Only needed inside a ``read_only=True``
        tool with an optional write mode; ``read_only=False`` tools get it
        automatically."""
        self._require_write(ctx)

    def token_is_rw(self, ctx: Any) -> bool:
        request = ctx.request_context.request
        return request is not None and getattr(request.state, "token_scope", None) == "rw"


def register_all(loaded: list[LoadedExtension], **api_kwargs: Any) -> list[LoadedExtension]:
    for ext in loaded:
        api = ExtensionAPI(ext, **api_kwargs)
        try:
            ext.module.register(api)
        except ExtensionError:
            raise
        except Exception as exc:
            raise ExtensionError(
                f"extension {ext.name!r}: register() raised {type(exc).__name__}: {exc}"
            ) from exc
        log.info("extension %s registered tools: %s", ext.name, ", ".join(ext.tools) or "(none)")
    return loaded


def info_block(loaded: list[LoadedExtension] | None) -> dict[str, Any]:
    """For get_folder_info and GET /v1/info."""
    return {
        "api_version": EXTENSION_API_VERSION,
        "modules": [{"module": e.name, "tools": list(e.tools)} for e in (loaded or [])],
    }
