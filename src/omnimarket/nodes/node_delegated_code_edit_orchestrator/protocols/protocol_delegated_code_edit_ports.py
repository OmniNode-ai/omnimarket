# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The seams the delegated code edit loop sequences (OMN-20290).

The orchestrator owns the order and every decision: which path may be written,
which check may run, when the loop stops. A port only does its one step. Tests
drive the orchestrator with fake ports; ``onex code-edit run`` binds them to
``onex delegate``, the worktree, and the declared checks
(``omnimarket.delegated_code_edit.loop_ports``).

Every filesystem port re-checks that the resolved path stays inside the
worktree, so a symlink cannot carry a read or a write out of it, even though
the orchestrator already refused any path that names one.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from omnimarket.nodes.node_delegated_code_edit_orchestrator.models.model_delegated_code_edit import (
    ModelCheckResult,
    ModelDeclaredCheck,
    ModelDelegatedCodeEditRequest,
    ModelTurnReply,
)


class LoopReceiptExistsError(RuntimeError):
    """A loop receipt already exists for this correlation id: a rerun is refused."""


class WorkspacePathError(ValueError):
    """A path resolves outside the worktree, or names nothing readable."""


@runtime_checkable
class ProtocolDelegatedCodeEditPorts(Protocol):
    def claim_loop_receipt(self, loop_run_id: str) -> None:
        """RAISE LoopReceiptExistsError when a receipt for this id exists."""
        ...

    def write_loop_receipt(
        self, loop_run_id: str, payload: dict[str, object]
    ) -> None: ...

    def workspace_files(
        self, request: ModelDelegatedCodeEditRequest
    ) -> tuple[tuple[str, int], ...]:
        """Tracked and untracked files of the worktree, with their line counts."""
        ...

    def read_file(self, request: ModelDelegatedCodeEditRequest, path: str) -> str:
        """RAISE WorkspacePathError for a path outside the worktree or not a file."""
        ...

    def list_dir(self, request: ModelDelegatedCodeEditRequest, path: str) -> str:
        """RAISE WorkspacePathError for a path outside the worktree or not a dir."""
        ...

    def grep(
        self, request: ModelDelegatedCodeEditRequest, pattern: str, path: str
    ) -> str:
        """Matching lines as ``path:line:text``. RAISE WorkspacePathError."""
        ...

    def write_file(
        self, request: ModelDelegatedCodeEditRequest, path: str, content: str
    ) -> None:
        """RAISE WorkspacePathError for a path outside the worktree."""
        ...

    def run_check(
        self, request: ModelDelegatedCodeEditRequest, check: ModelDeclaredCheck
    ) -> ModelCheckResult: ...

    def delegate(
        self,
        request: ModelDelegatedCodeEditRequest,
        prompt: str,
        response_contract: dict[str, object],
        turn: int,
    ) -> ModelTurnReply:
        """One ``onex delegate`` run; the reply's actions are already parsed."""
        ...

    def diff(self, request: ModelDelegatedCodeEditRequest) -> str:
        """The worktree's diff against its HEAD, untracked files included."""
        ...

    def score(
        self,
        request: ModelDelegatedCodeEditRequest,
        transcript: dict[str, object],
    ) -> dict[str, object]:
        """The tool_use rubric verdict record of this run's transcript."""
        ...


__all__ = [
    "LoopReceiptExistsError",
    "ProtocolDelegatedCodeEditPorts",
    "WorkspacePathError",
]
