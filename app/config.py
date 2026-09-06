"""Configuration, read once from the environment at startup."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer, got {raw!r}") from exc


def _env_set(name: str, default: set[str]) -> frozenset[str]:
    raw = os.environ.get(name)
    if raw is None:
        return frozenset(n.lower() for n in default)
    return frozenset(n.strip().lower() for n in raw.split(",") if n.strip())


@dataclass(frozen=True)
class Config:
    root: str = "/data"
    token: str = ""
    readonly_token: str = ""
    max_upload_bytes: int = 256 * 1024 * 1024
    max_list_entries: int = 10_000
    denied_names: frozenset[str] = field(default_factory=frozenset)
    allow_hardlinked_files: bool = False
    allow_delete: bool = True
    enable_docs: bool = True
    rate_limit_per_minute: int = 600
    require_mount: bool = True

    @classmethod
    def from_env(cls) -> "Config":
        token = os.environ.get("API_TOKEN", "").strip()
        if not token:
            raise RuntimeError(
                "API_TOKEN is required and must not be empty. "
                "Generate one with: openssl rand -hex 32"
            )
        if len(token) < 16:
            raise RuntimeError("API_TOKEN must be at least 16 characters")

        readonly = os.environ.get("API_TOKEN_READONLY", "").strip()
        if readonly and readonly == token:
            raise RuntimeError("API_TOKEN_READONLY must differ from API_TOKEN")
        if readonly and len(readonly) < 16:
            raise RuntimeError("API_TOKEN_READONLY must be at least 16 characters")

        return cls(
            root=os.environ.get("DATA_ROOT", "/data"),
            token=token,
            readonly_token=readonly,
            max_upload_bytes=_env_int("MAX_UPLOAD_BYTES", 256 * 1024 * 1024),
            max_list_entries=_env_int("MAX_LIST_ENTRIES", 10_000),
            denied_names=_env_set("DENIED_NAMES", {".git", ".gitmodules", ".ssh"}),
            allow_hardlinked_files=_env_bool("ALLOW_HARDLINKED_FILES", False),
            allow_delete=_env_bool("ALLOW_DELETE", True),
            enable_docs=_env_bool("ENABLE_DOCS", True),
            rate_limit_per_minute=_env_int("RATE_LIMIT_PER_MINUTE", 600),
            require_mount=_env_bool("REQUIRE_MOUNT", True),
        )
