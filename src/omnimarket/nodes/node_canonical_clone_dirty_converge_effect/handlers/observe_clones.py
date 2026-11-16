# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read every canonical clone under a registry root and say what an act run would do to it (OMN-24198).

The observe form of the dirty-converge tick. It runs no sync engine, no converge script and no fetch, and
every git call it makes is a read with optional locks off, so a clone is byte for byte as it was found.
The convergence orchestrator sends it while the host timers that run the act form still exist.

One line per clone, in the form the orchestrator's parser reads:

    CLEAN         <clone>: on main at the upstream tip
    WOULD_MOVE    <clone>: would fast-forward 3 commit(s) on main
    WOULD_REFUSE  <clone>: holds local work: modified 1 (a.py); an act run refuses it
    FAILED        <clone>: git status: <why>

A clone is a direct child of the registry root that holds a ``.git`` directory; a bare mirror has none and
is not read.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from omnimarket.nodes.node_canonical_clone_dirty_converge_effect.handlers.handler_canonical_clone_dirty_converge_effect import (
    HandlerCanonicalCloneDirtyConvergeEffect,
    _LocalWorkUnreadableError,
)
from omnimarket.nodes.node_canonical_clone_dirty_converge_effect.models.model_canonical_clone_dirty_converge import (
    ModelLocalWork,
)

CLEAN = "CLEAN"
WOULD_MOVE = "WOULD_MOVE"
WOULD_REFUSE = "WOULD_REFUSE"
FAILED = "FAILED"


def discover_clones(registry_root: Path) -> list[Path]:
    """The direct children of the registry root that hold a ``.git`` directory."""
    return sorted(
        child
        for child in registry_root.iterdir()
        if child.is_dir() and (child / ".git").is_dir()
    )


def _describe(work: tuple[ModelLocalWork, ...]) -> str:
    return "; ".join(
        f"{w.kind.value} {w.count}" + (f" ({', '.join(w.names)})" if w.names else "")
        for w in work
    )


def observe_clone(
    handler: HandlerCanonicalCloneDirtyConvergeEffect, clone: Path
) -> str:
    """One output line for one clone; a read that fails is a FAILED line, never a clean one."""

    def line(status: str, reason: str) -> str:
        return f"{status:13} {clone}: {reason}"

    try:
        work = handler.read_local_work(clone)
    except _LocalWorkUnreadableError as exc:
        return line(FAILED, str(exc))
    if work:
        return line(
            WOULD_REFUSE, f"holds local work: {_describe(work)}; an act run refuses it"
        )
    tracking = handler.declaration.tracking_branches
    branch = handler.read_out(clone, "symbolic-ref", "--quiet", "--short", "HEAD")
    if not branch:
        return line(
            WOULD_MOVE, "detached HEAD; an act run reattaches it to its tracking branch"
        )
    if branch not in tracking:
        return line(
            WOULD_MOVE,
            f"parked on {branch}; an act run returns it to its tracking branch",
        )
    code, count = handler.read_git(
        clone, "rev-list", "--count", f"HEAD..{branch}@{{u}}"
    )
    if code != 0:
        return line(FAILED, f"no upstream tip to compare with: {count}"[:300])
    if count.isdigit() and int(count) > 0:
        return line(WOULD_MOVE, f"would fast-forward {count} commit(s) on {branch}")
    return line(CLEAN, f"on {branch} at the upstream tip")


def observe(registry_root: Path, env: Mapping[str, str]) -> list[str]:
    """The output lines for every clone under the registry root, read-only."""
    handler = HandlerCanonicalCloneDirtyConvergeEffect(
        registry_root, env={**env, "GIT_OPTIONAL_LOCKS": "0"}
    )
    return [observe_clone(handler, clone) for clone in discover_clones(registry_root)]


__all__: list[str] = ["discover_clones", "observe", "observe_clone"]
