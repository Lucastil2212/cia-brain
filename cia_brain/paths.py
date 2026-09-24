"""Filesystem and digest validation for lake / worker payloads."""

from __future__ import annotations

import re
from pathlib import Path

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def require_sha256(value: str) -> str:
    digest = (value or "").strip().lower()
    if not SHA256_RE.fullmatch(digest):
        raise ValueError("invalid sha256 digest")
    return digest


def path_under(path: Path | str, root: Path) -> Path:
    """Resolve path and require it lives under root (after resolve)."""
    resolved = Path(path).resolve()
    root_r = root.resolve()
    if not resolved.is_relative_to(root_r):
        raise ValueError(f"path outside allowed root: {root_r}")
    return resolved


def normalized_cas_path(settings, digest: str) -> Path:
    digest = require_sha256(digest)
    return settings.normalized_dir / digest[:2] / f"{digest}.json.gz"
