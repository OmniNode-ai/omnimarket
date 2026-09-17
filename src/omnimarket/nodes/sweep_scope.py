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
import subprocess
from collections.abc import Iterable
from pathlib import Path, PurePosixPath

_log = logging.getLogger(__name__)

# Bound every git invocation so a wedged git can never hang a sweep. Enumerating
# a clone is a sub-second operation; a minute is three orders of magnitude of
# headroom and still terminates.
_GIT_TIMEOUT_SECONDS = 60

# Repository-pointer env vars that override ``-C <dir>``. A sweep run from a
# pre-commit or pre-push hook inherits these from the hook, so leaving them set
# would enumerate the HOOK's repository while reporting the verdict against the
# requested scan root — the same inherited-GIT_DIR class as OMN-18434. Scrubbed
# on every invocation so ``-C`` is the only thing that selects the repository.
_GIT_POINTER_ENV_VARS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
)

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


class SweepCorpusUnresolvedError(SweepScopeUnresolvedError):
    """Raised when a scan root's file corpus cannot be enumerated from git.

    Covers a root outside any git working tree, a missing ``git`` binary, and a
    git invocation that fails or times out. Every one of these is a hard error:
    falling back to a filesystem walk would make a correct run and a run over
    the wrong corpus indistinguishable in the output, which is exactly the
    silent-narrowing failure class the sweep shelf exists to catch.
    """


def _run_git(root: Path, args: list[str]) -> bytes:
    """Run a git command under ``root``, raising loudly on any failure.

    stderr is never discarded — it is folded into the raised message. A sweep
    that swallows git's own error text reports a confident zero it cannot
    justify (OMN-18472).
    """
    env = {k: v for k, v in os.environ.items() if k not in _GIT_POINTER_ENV_VARS}
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            check=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            env=env,
        )
    except FileNotFoundError as exc:
        raise SweepCorpusUnresolvedError(
            f"git is not on PATH — cannot enumerate the scan corpus under "
            f"{root}. Refusing to fall back to a filesystem walk, which would "
            f"scan a different corpus under the same verdict."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise SweepCorpusUnresolvedError(
            f"git {' '.join(args)} timed out after {_GIT_TIMEOUT_SECONDS}s "
            f"under {root} — cannot enumerate the scan corpus."
        ) from exc
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or b"").decode("utf-8", errors="replace").strip()
        raise SweepCorpusUnresolvedError(
            f"git {' '.join(args)} failed under {root} "
            f"(exit {exc.returncode}): {stderr or '<no stderr>'}"
        ) from exc
    return completed.stdout


def collect_git_corpus(
    root: str | os.PathLike[str],
    pattern: str,
) -> list[Path]:
    """Enumerate files matching ``pattern`` under ``root`` from git (OMN-18472).

    The corpus is the union of two git enumerations:

    * ``git ls-files -z`` — tracked files.
    * ``git ls-files -z --others --exclude-standard`` — untracked files that are
      not ignored.

    The union, not tracked-only, is the contract. A sweep running in pre-commit
    or in CI over a worktree must still see a NEW file the author has written
    but not yet ``git add``ed; a tracked-only corpus would report clean over
    exactly the code most likely to carry a fresh violation.

    What this drops relative to a filesystem walk is **gitignored paths only** —
    virtualenvs, build outputs, and staged copies of sibling repositories. Those
    are not source, and on ``omnibase_infra`` they are 17,288 of the 23,245
    ``*.py`` files a walk visits, every one of them under ``workspace/``, which
    ``stage_workspace.sh`` fills with copies of OTHER repositories' code.

    ``root`` may be a repository root or any subdirectory of one; git scopes the
    enumeration to it. Returned paths are absolute and sorted, so callers keep
    applying their own exclusion sets to the same absolute form a walk produced.

    Raises :class:`SweepCorpusUnresolvedError` when ``root`` is not inside a git
    working tree. There is no filesystem fallback — see the class docstring.
    """
    root_path = Path(root)
    if not root_path.is_dir():
        raise SweepCorpusUnresolvedError(
            f"scan root {root_path} is not a directory — cannot enumerate a "
            f"file corpus under it."
        )

    inside = _run_git(root_path, ["rev-parse", "--is-inside-work-tree"])
    if inside.decode("utf-8", errors="replace").strip() != "true":
        raise SweepCorpusUnresolvedError(
            f"scan root {root_path} is not inside a git working tree — "
            f"refusing to enumerate its corpus with a filesystem walk, which "
            f"would silently scan ignored build output under the same verdict."
        )

    raw = _run_git(root_path, ["ls-files", "-z"]) + _run_git(
        root_path, ["ls-files", "-z", "--others", "--exclude-standard"]
    )
    return _match_corpus(root_path, raw.split(b"\0"), pattern)


def _match_corpus(
    root_path: Path, entries: Iterable[bytes], pattern: str
) -> list[Path]:
    """Filter NUL-separated git path entries to ``pattern``, as absolute paths.

    ``git ls-files -z`` emits paths relative to ``root_path`` and does not apply
    ``core.quotePath`` escaping, so the bytes decode directly. Tracked entries
    whose file is absent from the working tree (a staged deletion) are dropped:
    a sweep reads file contents, and a path that cannot be read is not corpus.
    """
    matched: set[Path] = set()
    for entry in entries:
        if not entry:
            continue
        rel = entry.decode("utf-8", errors="surrogateescape")
        if not PurePosixPath(rel).match(pattern):
            continue
        candidate = root_path / rel
        if candidate.is_file():
            matched.add(candidate)
    return sorted(matched)


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
    "SweepCorpusUnresolvedError",
    "SweepScopeUnresolvedError",
    "collect_git_corpus",
    "require_target_dirs",
    "resolve_default_target_dirs",
    "resolve_omni_home",
    "resolve_repo_dirs",
]
