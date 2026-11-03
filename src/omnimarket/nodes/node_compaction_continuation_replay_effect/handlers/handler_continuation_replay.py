# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Continue a fixed model or replay recorded turns through isolated tool stubs."""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path

from ..models import (
    EnumRefusalReason,
    EnumReplayStatus,
    EnumToolOutcome,
    ModelRecordedCall,
    ModelReplayRequest,
    ModelReplayResult,
)
from ..models.model_experiment import (
    ModelAssistantTurn,
    ModelContinuationInput,
    ModelTurnFeedback,
)
from ..protocols.protocol_replay import ProtocolContinuationModel
from .confinement import confine_path
from .diff import collect_diff
from .guard import EgressBlockedError, deny_egress, file_digest
from .stubs import ProtocolToolStubs, SandboxTools, call_class_of, item_key_of

__all__ = ["HandlerContinuationReplay"]
StubFactory = Callable[[Path, dict[str, str | bytes]], ProtocolToolStubs]


class HandlerContinuationReplay:
    def __init__(
        self,
        stubs_factory: StubFactory | None = None,
        model: ProtocolContinuationModel | None = None,
        allowed_roots: Sequence[Path] = (),
    ) -> None:
        self._factory: StubFactory = stubs_factory or SandboxTools
        self._model = model
        self._roots = tuple(allowed_roots)

    def handle(self, request: ModelReplayRequest) -> ModelReplayResult:
        if self._model is not None and request.scripted_turns:
            raise ValueError("choose model continuation or recorded turns, not both")
        # Confine every command-supplied path before any read, copy, digest or diff.
        ledger_source = (
            confine_path("ledger_source", request.ledger_source, self._roots)
            if request.ledger_source
            else None
        )
        scratch_worktree = (
            confine_path("scratch_worktree", request.scratch_worktree, self._roots)
            if request.scratch_worktree
            else None
        )
        scratch_dir = Path(request.scratch_dir)
        scratch_dir.mkdir(parents=True, exist_ok=True)
        scratch_ledger = (
            Path(tempfile.mkdtemp(prefix="repeat-", dir=scratch_dir))
            / "ledger_scratch.md"
        )
        if request.ledger_content is not None:
            scratch_ledger.write_bytes(request.ledger_content)
        elif ledger_source:
            shutil.copyfile(ledger_source, scratch_ledger)
        else:
            scratch_ledger.touch()
        protected = [*request.protected_paths]
        if ledger_source:
            protected.append(str(ledger_source))
        before = {p: file_digest(p) for p in protected}
        calls: list[ModelRecordedCall] = []
        attempts: list[str] = []
        history: list[ModelTurnFeedback] = []
        failure: str | None = None
        with deny_egress(attempts, scratch_ledger):
            stubs = self._factory(
                scratch_ledger, {**request.frozen_files, **request.frozen_bytes}
            )
        for turn in range(1, request.n_turns + 1):
            if self._model is None:
                if turn > len(request.scripted_turns):
                    break
                proposed = ModelAssistantTurn(
                    calls=tuple(request.scripted_turns[turn - 1])
                )
            else:
                try:
                    proposed = self._model.continue_turn(
                        ModelContinuationInput(
                            model_id=request.model_id,
                            prompt=request.continuation_prompt,
                            context=request.context,
                            boundary_id=request.boundary_id,
                            arm=request.arm,
                            repeat_index=request.repeat_index,
                            turn=turn,
                            temperature=request.temperature,
                            history=tuple(history),
                        )
                    )
                    proposed = ModelAssistantTurn.model_validate(proposed)
                except Exception as error:
                    failure = f"{type(error).__name__}: {error}"
                    break
            recorded: list[ModelRecordedCall] = []
            for call in proposed.calls:
                try:
                    with deny_egress(attempts, scratch_ledger):
                        record = stubs.dispatch(turn, call)
                except EgressBlockedError as error:
                    record = ModelRecordedCall(
                        turn=turn,
                        call=call,
                        call_class=call_class_of(call),
                        item_key=item_key_of(call),
                        outcome=EnumToolOutcome.REFUSED,
                        refusal_reason=EnumRefusalReason.EGRESS_BLOCKED,
                        detail=str(error),
                    )
                recorded.append(record)
            calls.extend(recorded)
            history.append(ModelTurnFeedback(assistant=proposed, calls=tuple(recorded)))
        violations = [f"egress:{a}" for a in attempts]
        violations.extend(
            f"protected_file_changed:{p}"
            for p in protected
            if file_digest(p) != before[p]
        )
        status = EnumReplayStatus.COMPLETED
        if failure:
            status = EnumReplayStatus.UNAVAILABLE
        if violations:
            status = EnumReplayStatus.ISOLATION_BREACH
        return ModelReplayResult(
            status=status,
            boundary_id=request.boundary_id,
            arm=request.arm,
            repeat_index=request.repeat_index,
            turns_run=len(history),
            calls=calls,
            refusal_log=[c for c in calls if c.outcome == EnumToolOutcome.REFUSED],
            proposed_call_set=sorted({f"{c.call_class}|{c.item_key}" for c in calls}),
            scratch_ledger_path=str(scratch_ledger),
            scratch_rows=scratch_ledger.read_text(encoding="utf-8").splitlines(),
            isolation_violations=violations,
            diff=collect_diff(str(scratch_worktree)) if scratch_worktree else None,
            assistant_text=[t.assistant.text for t in history],
            failure_reason=failure,
        )
