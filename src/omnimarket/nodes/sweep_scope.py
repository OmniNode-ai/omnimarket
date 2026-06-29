# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared default-scope resolution for sweep nodes (OMN-13538).

Sweep nodes are enforcement gates. When invoked the operator-canonical no-arg
way (``onex skill <name>`` -> RuntimeLocal dispatch), the dispatch path does NOT
execute each node's ``__main__.py``. Historically every sweep kept its
``_DEFAULT_REPOS`` list and repo-name -> absolute-path resolution inside
``__main__.py`` only, so a no-arg dispatch defaulted its scan scope to ``[]`` and
returned ``status=clean / findings=[]`` — a trust-defeating **false-clean**
(Rule 5: an enforcement gate that silently passes is worse than no gate).

This module owns the canonical default-repo set and the resolution helpers so
the ``__main__`` CLI path and the RuntimeLocal dispatch path resolve identically.
It is a leaf module (no node imports another node's private handler — see
omnimarket CLAUDE.md boundary rules).

**Fail-loud contract (Rule 5):** when scan scope is empty AND no default can be
resolved, callers MUST surface an error / non-zero status — never ``clean``.
:func:`require_target_dirs` raises :class:`SweepScopeUnresolvedError` for exactly
that case; ``"scanned 0 repos"`` must never be a pass.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

_log = logging.getLogger(__name__)

# Canonical default handler-repo set scanned when no explicit scope is supplied.
# Kept in lockstep with the per-node ``__main__`` lists this module replaces so a
# no-arg dispatch scans the real handler universe (OMN-13538). omnibase_compat is
# included so wire-DTO / primitive handlers are not silently skipped.
DEFAULT_REPOS: tuple[str, ...] = (
    "omniclaude",
    "omnibase_core",
    "omnibase_infra",
    "omnibase_spi",
    "omnibase_compat",
    "omniintelligence",
    "omnimemory",
    "onex_change_control",
)


class SweepScopeUnresolvedError(RuntimeError):
    """Raised when an empty scan scope cannot be resolved to any directory.

    Surfacing this (instead of silently scanning zero repos) is the Rule-5
    fail-loud guarantee: a sweep that cannot determine WHAT to scan must error,
    never return a clean verdict.
    """


def resolve_omni_home(explicit: str | os.PathLike[str] | None = None) -> str:
    """Resolve ``$OMNI_HOME``, failing loud when it cannot be determined.

    Precedence: an explicit non-empty value, then the ``OMNI_HOME`` env var.
    Raises :class:`SweepScopeUnresolvedError` when neither yields a value — a
    silent default would reintroduce the cross-machine breakage Rule 8 forbids.
    """
    candidate = str(explicit) if explicit else os.environ.get("OMNI_HOME")
    if not candidate:
        raise SweepScopeUnresolvedError(
            "OMNI_HOME is not set and no explicit omni_home was supplied — "
            "cannot resolve the default repo scope. Set OMNI_HOME or pass "
            "explicit scan targets. A sweep must never silently scan zero repos."
        )
    return candidate


def resolve_repo_dirs(
    repos: list[str] | tuple[str, ...],
    omni_home: str | os.PathLike[str],
) -> list[str]:
    """Resolve bare repo names to absolute directories under ``omni_home``.

    Missing repos are logged, not silently dropped into a wrong default.
    Returns only directories that exist.
    """
    root = Path(omni_home)
    resolved: list[str] = []
    for repo in repos:
        candidate = root / repo
        if candidate.is_dir():
            resolved.append(str(candidate))
        else:
            _log.warning("repo dir not found: %s", candidate)
    return resolved


def resolve_default_target_dirs(
    explicit_target_dirs: list[str] | None,
    repos: list[str] | None,
    omni_home: str | os.PathLike[str] | None = None,
) -> list[str]:
    """Resolve the absolute scan directories for a sweep request.

    Precedence (identical for ``__main__`` and the RuntimeLocal dispatch path):

    1. ``explicit_target_dirs`` — already-absolute paths, returned as-is.
    2. ``repos`` — bare repo names resolved against ``omni_home``.
    3. :data:`DEFAULT_REPOS` resolved against ``omni_home`` when both are empty.

    ``omni_home`` is resolved via :func:`resolve_omni_home` (which fails loud)
    only when repo-name resolution is actually needed — explicit absolute
    ``target_dirs`` never require it.
    """
    if explicit_target_dirs:
        return list(explicit_target_dirs)

    resolved_home = resolve_omni_home(omni_home)
    names = list(repos) if repos else list(DEFAULT_REPOS)
    return resolve_repo_dirs(names, resolved_home)


def require_target_dirs(
    explicit_target_dirs: list[str] | None,
    repos: list[str] | None,
    omni_home: str | os.PathLike[str] | None = None,
) -> list[str]:
    """Resolve scan dirs, raising when the result would be empty (Rule 5).

    Wraps :func:`resolve_default_target_dirs` and converts an empty resolution
    (e.g. ``$OMNI_HOME`` set but none of the default repo dirs exist) into a
    loud :class:`SweepScopeUnresolvedError` instead of a silent zero-scan.
    """
    target_dirs = resolve_default_target_dirs(explicit_target_dirs, repos, omni_home)
    if not target_dirs:
        raise SweepScopeUnresolvedError(
            "Resolved an empty scan scope — no valid repo directories were "
            "found for the requested/default repo set. Refusing to report a "
            "clean verdict over zero repos (Rule 5: a gate that silently "
            "passes is worse than no gate)."
        )
    return target_dirs


__all__ = [
    "DEFAULT_REPOS",
    "SweepScopeUnresolvedError",
    "require_target_dirs",
    "resolve_default_target_dirs",
    "resolve_omni_home",
    "resolve_repo_dirs",
]
