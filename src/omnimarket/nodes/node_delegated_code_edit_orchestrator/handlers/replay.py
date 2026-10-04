# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Rebuild a code edit loop's worktree from its receipt.

``replay_worktree`` copies the base tree, then re-applies each turn's recorded
writing actions through the loop handler's own apply step
(``HandlerDelegatedCodeEditOrchestrator.apply_action``), so an edit that the
live loop shifted by indentation, skipped as already applied or spread over a
glob is replayed with exactly those semantics. Every action's recorded
sha256 of the files it wrote is the check that replay equals the live loop: a
difference raises ``ReplayMismatchError`` and nothing after that action runs.

Reads, checks and ``finish`` change no file and are not replayed. A ``format``
action runs the request's formatter, so replaying one needs the caller to pass
the ``run_check`` that runs it. A receipt that keeps no action arguments (v1,
or a v2 receipt resumed from a v1 one) is refused before anything is written.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from omnimarket.nodes.node_delegated_code_edit_orchestrator.handlers.handler_delegated_code_edit_orchestrator import (
    RECEIPT_SCHEMA,
    RECEIPT_SCHEMA_V1,
    HandlerDelegatedCodeEditOrchestrator,
)
from omnimarket.nodes.node_delegated_code_edit_orchestrator.models.model_delegated_code_edit import (
    WRITING_TOOLS,
    EnumCodeEditTool,
    ModelCheckResult,
    ModelCodeEditAction,
    ModelDeclaredCheck,
    ModelDelegatedCodeEditRequest,
)
from omnimarket.nodes.node_delegated_code_edit_orchestrator.protocols.protocol_delegated_code_edit_ports import (
    ProtocolDelegatedCodeEditPorts,
    WorkspacePathError,
)

RunCheck = Callable[
    [ModelDelegatedCodeEditRequest, ModelDeclaredCheck], ModelCheckResult
]


class ReplayError(Exception):
    """Base of every replay failure."""


class ReplayRefusedError(ReplayError):
    """The receipt or the arguments cannot be replayed; nothing was written."""


class ReplayMismatchError(ReplayError):
    """Replay produced something other than what the live loop recorded."""

    def __init__(
        self, *, turn: int, action: int, path: str, expected: str, actual: str
    ) -> None:
        super().__init__(
            f"turn {turn} action {action}: replay differs from the live loop at "
            f"{path or 'the action result'}: recorded {expected or '(none)'}, "
            f"replayed {actual or '(none)'}"
        )
        self.turn = turn
        self.action = action
        self.path = path
        self.expected = expected
        self.actual = actual


@dataclass(frozen=True)
class ReplayResult:
    """The worktree at the end of ``through_turn``: the sha256 of every file the
    replayed actions wrote (its last write wins), keyed by path."""

    through_turn: int
    files: dict[str, str] = field(default_factory=dict)


class _ReplayPorts:
    """The three ports the apply step uses, bound to the replay worktree."""

    def __init__(self, root: Path, run_check: RunCheck | None) -> None:
        self._root = root.resolve()
        self._run_check = run_check

    def _inside(self, rel: str) -> Path:
        candidate = (self._root / rel).resolve()
        if candidate != self._root and self._root not in candidate.parents:
            raise WorkspacePathError(f"{rel} resolves outside the worktree")
        if ".git" in candidate.relative_to(self._root).parts:
            raise WorkspacePathError(f"{rel} is inside .git")
        return candidate

    def read_file(self, request: ModelDelegatedCodeEditRequest, path: str) -> str:
        target = self._inside(path)
        if not target.is_file():
            raise WorkspacePathError(f"{path} is not a file")
        return target.read_text(encoding="utf-8", errors="replace")

    def write_file(
        self, request: ModelDelegatedCodeEditRequest, path: str, content: str
    ) -> None:
        target = self._inside(path)
        if target.is_symlink() or target.is_dir():
            raise WorkspacePathError(f"{path} is a symlink or a directory")
        target.parent.mkdir(parents=True, exist_ok=True)
        self._inside(path)
        target.write_text(content, encoding="utf-8")

    def run_check(
        self, request: ModelDelegatedCodeEditRequest, check: ModelDeclaredCheck
    ) -> ModelCheckResult:
        if self._run_check is None:
            raise ReplayRefusedError(f"{check.name} needs a run_check to replay")
        return self._run_check(request, check)


def _manifest(base_root: Path) -> tuple[str, ...]:
    return tuple(
        sorted(
            path.relative_to(base_root).as_posix()
            for path in base_root.rglob("*")
            if path.is_file() and ".git" not in path.relative_to(base_root).parts
        )
    )


def _replayable(
    receipt: dict[str, object], through_turn: int, run_check: RunCheck | None
) -> list[dict[str, object]]:
    """The turns to replay, refused up front when any action cannot be."""
    schema = receipt.get("schema")
    if schema == RECEIPT_SCHEMA_V1:
        raise ReplayRefusedError(
            "a v1 receipt keeps no action arguments, so its turns cannot be replayed"
        )
    if schema != RECEIPT_SCHEMA:
        raise ReplayRefusedError(f"unknown receipt schema {schema!r}")
    turns = cast("list[dict[str, object]]", receipt.get("turns", []))
    last = max((cast(int, t["turn"]) for t in turns), default=0)
    if not 0 <= through_turn <= last:
        raise ReplayRefusedError(
            f"through_turn {through_turn} is outside the receipt's turns 0..{last}"
        )
    chosen = [t for t in turns if cast(int, t["turn"]) <= through_turn]
    for turn in chosen:
        for number, action in enumerate(
            cast("list[dict[str, object]]", turn["actions"]), start=1
        ):
            if "arguments" not in action or "written_sha256" not in action:
                raise ReplayRefusedError(
                    f"turn {turn['turn']} action {number} predates recorded "
                    "arguments (a v1 turn in a resumed receipt)"
                )
            if (
                action["tool"] == EnumCodeEditTool.FORMAT.value
                and not action["refused"]
                and run_check is None
            ):
                raise ReplayRefusedError(
                    f"turn {turn['turn']} action {number} is a format action; "
                    "replaying it needs run_check"
                )
    return chosen


def replay_worktree(
    receipt: dict[str, object],
    base_root: Path,
    target_root: Path,
    *,
    through_turn: int,
    run_check: RunCheck | None = None,
) -> ReplayResult:
    """Rebuild into ``target_root`` the worktree as it stood at the end of
    ``through_turn`` (0 is the base tree), from ``base_root`` and a v2 receipt.

    ``base_root`` is the tree the loop started from and is only read.
    ``target_root`` must be absent or empty.
    """
    chosen = _replayable(receipt, through_turn, run_check)
    if not base_root.is_dir():
        raise ReplayRefusedError(f"base_root {base_root} is not a directory")
    if target_root.exists() and (
        not target_root.is_dir() or any(target_root.iterdir())
    ):
        raise ReplayRefusedError(f"target_root {target_root} is not empty")
    paths = _manifest(base_root)
    shutil.copytree(
        base_root,
        target_root,
        ignore=shutil.ignore_patterns(".git"),
        dirs_exist_ok=True,
    )
    request = ModelDelegatedCodeEditRequest.model_validate(receipt["request"])
    handler = HandlerDelegatedCodeEditOrchestrator(
        cast(ProtocolDelegatedCodeEditPorts, _ReplayPorts(target_root, run_check))
    )
    files: dict[str, str] = {}
    for turn in chosen:
        number = cast(int, turn["turn"])
        for index, recorded in enumerate(
            cast("list[dict[str, object]]", turn["actions"]), start=1
        ):
            tool = EnumCodeEditTool(cast(str, recorded["tool"]))
            if tool not in WRITING_TOOLS:
                continue
            action = ModelCodeEditAction.model_validate(
                {"tool": tool.value, **cast("dict[str, object]", recorded["arguments"])}
            )
            observation, written = handler.apply_action(request, action, paths)
            for name in ("ok", "refused"):
                expected, actual = (
                    bool(recorded[name]),
                    bool(getattr(observation, name)),
                )
                if expected != actual:
                    raise ReplayMismatchError(
                        turn=number,
                        action=index,
                        path="",
                        expected=f"{name}={expected}",
                        actual=f"{name}={actual}",
                    )
            expected_files = cast("dict[str, str]", recorded["written_sha256"])
            for path in sorted({*expected_files, *written}):
                if expected_files.get(path, "") != written.get(path, ""):
                    raise ReplayMismatchError(
                        turn=number,
                        action=index,
                        path=path,
                        expected=expected_files.get(path, ""),
                        actual=written.get(path, ""),
                    )
            files.update(written)
    return ReplayResult(through_turn=through_turn, files=files)


__all__ = [
    "ReplayError",
    "ReplayMismatchError",
    "ReplayRefusedError",
    "ReplayResult",
    "replay_worktree",
]
