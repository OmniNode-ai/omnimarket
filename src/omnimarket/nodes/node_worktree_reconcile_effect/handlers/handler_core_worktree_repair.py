# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unset a canonical clone's ``core.worktree`` when it names a directory that is gone."""

from pathlib import Path
from typing import Literal, Protocol

from omnimarket.nodes.node_worktree_reconcile_effect.models.model_core_worktree_repair import (
    EnumCoreWorktreeRepairKind,
    ModelCoreWorktreeRepair,
    ModelCoreWorktreeRepairRequest,
    ModelCoreWorktreeRepairResult,
)
from omnimarket.nodes.node_worktree_reconcile_effect.protocols import ProtocolClock


class ProtocolCoreWorktreeConfig(Protocol):
    def read(self, path: Path) -> str | None: ...
    def unset(self, path: Path) -> None: ...


class ProtocolStatusRowWriter(Protocol):
    def write(self, row: str) -> None: ...


def missing_directory(clone: Path, value: str) -> str | None:
    """The directory a ``core.worktree`` value names when it is gone, else None.

    A relative value is relative to the gitdir. A value naming the clone itself, or any
    existing directory, is not this repair's to touch."""
    named = Path(value)
    resolved = (named if named.is_absolute() else clone / ".git" / named).resolve()
    if resolved == clone.resolve() or resolved.exists():
        return None
    return str(resolved)


def status_row(
    request: ModelCoreWorktreeRepairRequest,
    repair: ModelCoreWorktreeRepair,
    stamp: str,
) -> str:
    return (
        f"{stamp} | STATUS | lane={request.lane} | host={request.host}"
        f" | repair_kind={repair.repair_kind.value} | clone={repair.clone_path}"
        f" | missing_directory={repair.missing_directory}"
        " | core.worktree named a missing directory and was unset with git config --file"
    )


class HandlerCoreWorktreeRepair:
    def __init__(
        self,
        *,
        config: ProtocolCoreWorktreeConfig,
        writer: ProtocolStatusRowWriter | None,
        clock: ProtocolClock,
    ) -> None:
        self._config = config
        self._writer = writer
        self._clock = clock

    @property
    def handler_type(self) -> Literal["NODE_HANDLER"]:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> Literal["EFFECT"]:
        return "EFFECT"

    def handle(
        self, request: ModelCoreWorktreeRepairRequest
    ) -> ModelCoreWorktreeRepairResult:
        clones = sorted(
            child
            for registry in request.registries
            if Path(registry).is_dir()
            for child in Path(registry).iterdir()
            if (child / ".git" / "config").is_file()
        )
        repairs: list[ModelCoreWorktreeRepair] = []
        for clone in clones:
            repair = self._repair_one(request, clone)
            if repair is not None:
                repairs.append(repair)
        return ModelCoreWorktreeRepairResult(
            host=request.host,
            scanned=len(clones),
            repairs=tuple(repairs),
            failures=sum(r.error is not None for r in repairs),
        )

    def _repair_one(
        self, request: ModelCoreWorktreeRepairRequest, clone: Path
    ) -> ModelCoreWorktreeRepair | None:
        kind = EnumCoreWorktreeRepairKind.UNSET_MISSING_CORE_WORKTREE
        try:
            value = self._config.read(clone)
            gone = missing_directory(clone, value) if value else None
        except Exception as exc:
            return ModelCoreWorktreeRepair(
                clone_path=str(clone),
                missing_directory="",
                repair_kind=kind,
                outcome="failed",
                error=type(exc).__name__,
            )
        if gone is None:
            return None
        outcome: Literal["detected", "repaired", "failed"] = "detected"
        error = None
        if request.execute:
            try:
                self._config.unset(clone)
                outcome = "repaired"
            except Exception as exc:
                outcome = "failed"
                error = type(exc).__name__
        repair = ModelCoreWorktreeRepair(
            clone_path=str(clone),
            missing_directory=gone,
            repair_kind=kind,
            outcome=outcome,
            error=error,
        )
        if outcome != "repaired":
            return repair
        stamp = self._clock.now().strftime("%Y-%m-%dT%H:%M:%SZ")
        row = status_row(request, repair, stamp)
        written = False
        if self._writer is not None:
            try:
                self._writer.write(row)
                written = True
            except Exception as exc:
                error = f"row_not_written:{type(exc).__name__}"
        return repair.model_copy(
            update={"status_row": row, "row_written": written, "error": error}
        )
