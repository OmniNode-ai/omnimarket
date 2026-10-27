# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Render the shell lines that set a Codex lane's worktree ``.git`` link aside and put it back (OMN-17427).

Codex makes the gitdir a cwd's ``.git`` link names read-only whatever the writable roots say, so the remote-lane runner sets the link
aside for the run and points git at the same gitdir through ``GIT_DIR`` and ``GIT_WORK_TREE``. The lines read ``$W`` (the worktree) and
``$C`` (the lane's context directory) from the script they are placed in.
"""

from __future__ import annotations

from ..models.model_codex_gitlink import (
    ModelCodexGitlinkRequest,
    ModelCodexGitlinkResult,
)


def core_worktree_repair_line() -> str:
    """Unset the ``core.worktree`` the run left in its clone's shared config, when it names this run's worktree.

    The aside link exports ``GIT_DIR`` (the worktree's gitdir) and ``GIT_WORK_TREE`` (its tree); any ``git init`` the engine then runs,
    anywhere, writes ``core.worktree`` = the run's tree into the clone's shared ``.git/config``. Every git command on the canonical
    clone then read the run's directory (2026-10-18: ``git status`` clean over a stale tree, a convergence reset the run directory,
    the plugin tick crashed for every session). Only a value naming this ``$W`` is unset: a ``core.worktree`` that names anything
    else is not this run's to touch (the canonical-clone guard and the host parity check refuse it)."""
    return (
        'G=$(env -u GIT_DIR -u GIT_WORK_TREE git -C "$W" rev-parse --path-format=absolute --git-common-dir 2>/dev/null); '
        '[ -n "$G" ] && V=$(git config --file "$G/config" --get core.worktree 2>/dev/null) && [ -n "$V" ] && '
        '[ "$(cd "$V" 2>/dev/null && pwd -P)" = "$(cd "$W" 2>/dev/null && pwd -P)" ] && '
        'git config --file "$G/config" --unset core.worktree'
    )


def aside_line(request: ModelCodexGitlinkRequest) -> str:
    """Lock the worktree, set its ``.git`` link aside and point git at its gitdir."""
    return (
        f'if [ -n "$GD" ] && [ -f "$W/.git" ]; then printf "{request.lock_reason}\\n" > "$GD/locked" || exit 98; '
        f'mv "$W/.git" "$C/{request.gitlink}" || exit 98; export GIT_DIR="$GD" GIT_WORK_TREE="$W"; fi'
    )


def put_back_line(request: ModelCodexGitlinkRequest) -> str:
    """Put the ``.git`` link back for git run outside the sandbox; the lock stays until ``restore_line``. The sandbox leaves an empty
    ``.git`` directory behind as its mount point (a sandboxed lane: three commits failed "not a git repository" on it), so an empty
    one is removed first; a non-empty one never is.

    The sandbox likewise leaves empty ``.agents``, ``.aws`` and ``.codex`` mount points (lane next3-OMN-19232-2a21-cx failed three
    commits on them, the repository's untracked-file validator refusing them). Each one that is an empty directory and that git does
    not track is removed, so the commit hooks never see it; ``rmdir`` leaves a directory with anything in it. The run's stray
    ``core.worktree`` is unset here too, before any commit."""
    return (
        f'[ -f "$C/{request.gitlink}" ] && [ -d "$W/.git" ] && rmdir "$W/.git" 2>/dev/null; '
        f'[ -f "$C/{request.gitlink}" ] && [ ! -e "$W/.git" ] && mv "$C/{request.gitlink}" "$W/.git"; '
        "unset GIT_DIR GIT_WORK_TREE; "
        f"{core_worktree_repair_line()}; "
        f'for m in {" ".join(request.mounts)}; do [ -d "$W/$m" ] && [ ! -L "$W/$m" ] && '
        '[ -z "$(git -C "$W" ls-files -- "$m" 2>/dev/null)" ] && rmdir "$W/$m" 2>/dev/null; done; true'
    )


def restore_line(request: ModelCodexGitlinkRequest) -> str:
    """Put the worktree's ``.git`` link back, unlock the worktree (only a lock this engine wrote) and unset the run's stray
    ``core.worktree``. Run after Codex, and again by the cleanup step."""
    return (
        f'{{ [ -f "$C/{request.gitlink}" ] && [ -d "$W/.git" ] && rmdir "$W/.git" 2>/dev/null; '
        f'[ -f "$C/{request.gitlink}" ] && [ ! -e "$W/.git" ] && mv "$C/{request.gitlink}" "$W/.git"; '
        f'L=$(env -u GIT_DIR -u GIT_WORK_TREE git -C "$W" rev-parse --path-format=absolute --git-dir '
        f'2>/dev/null)/locked; grep -qx "{request.lock_reason}" "$L" 2>/dev/null && rm -f "$L"; '
        f"{core_worktree_repair_line()}; }}"
    )


class HandlerCodexGitlink:
    def handle(self, request: ModelCodexGitlinkRequest) -> ModelCodexGitlinkResult:
        return ModelCodexGitlinkResult(
            aside_line(request), put_back_line(request), restore_line(request)
        )
