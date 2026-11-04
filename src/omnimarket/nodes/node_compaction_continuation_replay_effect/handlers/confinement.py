# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Confine command-supplied paths to the roots the runtime overlay injects.

The ledger source, tolerance file and scratch worktree arrive on the command payload. A path
outside the configured roots is refused before any read, so no content of an outside file can
reach the completed topic. No roots configured means no path is accepted.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

__all__ = ["PathConfinementError", "confine_path", "resolve_roots"]


class PathConfinementError(ValueError):
    """A command-supplied path is outside the configured roots."""


def resolve_roots(roots: Sequence[Path | str]) -> tuple[Path, ...]:
    return tuple(Path(r).resolve() for r in roots)


def confine_path(field: str, path: str, roots: Sequence[Path | str]) -> Path:
    """Return the resolved path, or raise before anything is opened.

    Symlinks and `..` are resolved first; a `..` component is refused outright as well, so a
    payload never carries a traversal spelling.
    """
    candidate = Path(path)
    if ".." in candidate.parts:
        raise PathConfinementError(f"{field} must not contain '..'")
    resolved = candidate.resolve()
    if not any(resolved.is_relative_to(root) for root in resolve_roots(roots)):
        raise PathConfinementError(f"{field} is outside the configured input roots")
    return resolved
