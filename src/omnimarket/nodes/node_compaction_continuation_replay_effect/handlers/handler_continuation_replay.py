# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Replay one repeat of a compacted context's continuation with every tool stubbed.

The proposed calls are the product: each is recorded with its call class and item key, and a
refused call is logged. The turns come from the request (recorded model output), so replay
never re-calls a model. Isolation is audited, not assumed: socket and subprocess attempts made
while the stubs run are blocked and recorded, and every protected real file is digested before
and after. Any violation makes the result ISOLATION_BREACH.

Model-before-build: no lease, one writer (the scratch ledger copy), no terminal of its own;
the replay-run lifecycle belongs to the CM.0 model.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path

from omnimarket.nodes.node_compaction_continuation_replay_effect.handlers.diff import (
    collect_diff,
)
from omnimarket.nodes.node_compaction_continuation_replay_effect.handlers.guard import (
    EgressBlockedError,
    deny_egress,
    file_digest,
)
from omnimarket.nodes.node_compaction_continuation_replay_effect.handlers.stubs import (
    ProtocolToolStubs,
    SandboxTools,
    call_class_of,
    item_key_of,
)
from omnimarket.nodes.node_compaction_continuation_replay_effect.models import (
    EnumRefusalReason,
    EnumReplayStatus,
    EnumToolOutcome,
    ModelRecordedCall,
    ModelReplayRequest,
    ModelReplayResult,
)

__all__ = ["HandlerContinuationReplay"]

StubFactory = Callable[[Path, dict[str, str]], ProtocolToolStubs]


class HandlerContinuationReplay:
    def __init__(self, stubs_factory: StubFactory | None = None) -> None:
        self._factory: StubFactory = stubs_factory or SandboxTools

    def handle(self, request: ModelReplayRequest) -> ModelReplayResult:
        scratch_dir = Path(request.scratch_dir)
        scratch_dir.mkdir(parents=True, exist_ok=True)
        scratch_ledger = scratch_dir / "ledger_scratch.md"
        if request.ledger_source:
            shutil.copyfile(request.ledger_source, scratch_ledger)
        else:
            scratch_ledger.touch()

        protected = [*request.protected_paths]
        if request.ledger_source:
            protected.append(request.ledger_source)
        before = {p: file_digest(p) for p in protected}

        stubs = self._factory(scratch_ledger, dict(request.frozen_files))
        calls: list[ModelRecordedCall] = []
        attempts: list[str] = []
        turns_run = 0
        with deny_egress(attempts):
            for turn, proposed in enumerate(
                request.scripted_turns[: request.n_turns], start=1
            ):
                turns_run = turn
                for call in proposed:
                    try:
                        calls.append(stubs.dispatch(turn, call))
                    except EgressBlockedError as exc:
                        calls.append(
                            ModelRecordedCall(
                                turn=turn,
                                call=call,
                                call_class=call_class_of(call),
                                item_key=item_key_of(call),
                                outcome=EnumToolOutcome.REFUSED,
                                refusal_reason=EnumRefusalReason.EGRESS_BLOCKED,
                                detail=str(exc),
                            )
                        )

        violations = [f"egress:{a}" for a in attempts]
        violations += [
            f"protected_file_changed:{p}"
            for p in protected
            if file_digest(p) != before[p]
        ]
        rows = scratch_ledger.read_text(encoding="utf-8").splitlines()
        return ModelReplayResult(
            status=EnumReplayStatus.ISOLATION_BREACH
            if violations
            else EnumReplayStatus.COMPLETED,
            boundary_id=request.boundary_id,
            arm=request.arm,
            repeat_index=request.repeat_index,
            turns_run=turns_run,
            calls=calls,
            refusal_log=[c for c in calls if c.outcome == EnumToolOutcome.REFUSED],
            proposed_call_set=sorted({f"{c.call_class}|{c.item_key}" for c in calls}),
            scratch_ledger_path=str(scratch_ledger),
            scratch_rows=rows,
            isolation_violations=violations,
            diff=collect_diff(request.scratch_worktree)
            if request.scratch_worktree
            else None,
        )
