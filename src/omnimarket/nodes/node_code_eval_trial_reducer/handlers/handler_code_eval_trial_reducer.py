# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerCodeEvalTrialReducer: the pure transition function of one code eval run.

Definition-B: ``handle(ModelCodeEvalReduceInput) -> ModelCodeEvalReduceOutput``.
No clock (time is the input's ``now``), no I/O, no language model. Exemplar:
``node_pr_landing_reducer`` (stored row plus one message to the next row plus
emissions). The guards are those of formal/code_eval_trial/CodeEvalTrial.tla:

* a message with no row, for a closed run, or naming a unit or score not in
  flight changes nothing (a late answer changes nothing);
* one score is in flight at a time, the oldest queued is sent when none is;
* a try is placed only after its task's control reads ``pass``;
* a sequence ends at the first ``pass`` or at the largest attempt budget; an
  ``unscorable`` try takes its ordinal and the sequence goes on;
* the nudge expires a try or score in flight past its timeout plus the slack
  as ``unscorable(harness)`` (a control that expires could not be run);
* the run closes once, on the leg where every admitted task has its control
  verdict and every sequence has ended, and a closed row is frozen.

The place step runs on every leg: each pending try takes one of the leg's free
hosts, which then leave the snapshot.
"""

from __future__ import annotations

import hashlib
from datetime import datetime

from omnimarket.nodes.node_code_contract_score_effect.models.enum_code_score import (
    EnumCodeScoreSource,
    EnumCodeScoreVerdict,
)
from omnimarket.nodes.node_code_eval_trial_orchestrator.models import (
    EnumCodeEvalSlot,
    ModelCodeEvalControlReading,
    ModelCodeEvalRunClosed,
    ModelCodeEvalRunRow,
    ModelCodeEvalSequenceReading,
    ModelCodeEvalSequenceRow,
    ModelCodeEvalSettings,
    ModelCodeEvalTaskRow,
    ModelCodeEvalVerdict,
)
from omnimarket.nodes.node_code_eval_trial_reducer.models import (
    ModelCodeEvalReduceInput,
    ModelCodeEvalReduceOutput,
    ModelNudgeMessage,
    ModelRunRequestedMessage,
    ModelScoreAnsweredMessage,
    ModelScoreToSend,
    ModelUnitAnsweredMessage,
    ModelUnitFailedMessage,
    ModelUnitToPlace,
    ModelVerdictRow,
)

_PASS = EnumCodeScoreVerdict.PASS
_UNSCORABLE = EnumCodeScoreVerdict.UNSCORABLE
_HARNESS = ModelCodeEvalVerdict(verdict=_UNSCORABLE, source=EnumCodeScoreSource.HARNESS)
_TRUNCATED = ModelCodeEvalVerdict(
    verdict=_UNSCORABLE, source=EnumCodeScoreSource.DRAFT_NOT_RECOVERABLE
)
#: Loop results that are a turn off the pin or two failed delegate runs (rule 2).
_LOOP_NOT_RUN = frozenset({"infra_error", "delegate_failed"})


class CodeEvalConfigurationError(ValueError):
    """The run's timeouts plus slack do not fit under the completion bound (dead-lettered, no row)."""


def unit_id_for(run_id: object, task_key: str, model: str, ordinal: int) -> str:
    """A fixed-length hash of run, sequence and ordinal: a plain name the unit contract accepts."""
    digest = hashlib.sha256(
        f"{run_id}|{task_key}|{model}|{ordinal}".encode()
    ).hexdigest()
    return f"lw-{digest[:24]}"


class _Leg:
    """One leg's working copy of the row and what it emits."""

    def __init__(self, row: ModelCodeEvalRunRow, now: datetime, hosts: tuple[str, ...]):
        self.row = row
        self.now = now
        self.hosts = list(hosts)
        self.scores: list[ModelScoreToSend] = []
        self.units: list[ModelUnitToPlace] = []
        self.rows: list[ModelVerdictRow] = []
        self.closed: ModelCodeEvalRunClosed | None = None


class HandlerCodeEvalTrialReducer:
    """REDUCER handler: stored row plus one message to the next row plus emissions."""

    def handle(self, request: ModelCodeEvalReduceInput) -> ModelCodeEvalReduceOutput:
        message = request.message
        if isinstance(message, ModelRunRequestedMessage):
            return self._run(request, message)
        stored = request.row
        if stored is None:
            return self._dropped(None, "no_row")
        if stored.closed:
            return self._dropped(stored, "run_closed")
        leg = _Leg(stored.model_copy(deep=True), request.now, request.free_hosts)
        if isinstance(message, ModelUnitAnsweredMessage):
            transition = self._unit_answered(leg, message)
        elif isinstance(message, ModelUnitFailedMessage):
            transition = self._unit_failed(leg, message.unit_id)
        elif isinstance(message, ModelScoreAnsweredMessage):
            transition = self._score_answered(leg, message)
        elif isinstance(message, ModelNudgeMessage):
            transition = self._nudge(leg)
        else:  # pragma: no cover - the union is closed
            raise TypeError(type(message).__name__)
        if transition.startswith("dropped:"):
            return self._dropped(stored, transition.removeprefix("dropped:"))
        return self._finish(stored, leg, transition)

    # -- run ------------------------------------------------------------

    def _run(
        self, request: ModelCodeEvalReduceInput, message: ModelRunRequestedMessage
    ) -> ModelCodeEvalReduceOutput:
        if request.row is not None:
            return self._dropped(request.row, "run_already_started")
        self._check_bound(message.settings)
        run_id = message.request.correlation_id
        excluded: dict[str, int] = {}
        tasks: list[ModelCodeEvalTaskRow] = []
        for resolved in message.tasks:
            if resolved.excluded_reason:
                excluded[resolved.excluded_reason] = (
                    excluded.get(resolved.excluded_reason, 0) + 1
                )
            else:
                tasks.append(
                    ModelCodeEvalTaskRow(
                        task_key=resolved.task_key,
                        band=resolved.band,
                        control_queued_seq=len(tasks) + 1,
                    )
                )
        row = ModelCodeEvalRunRow(
            run_id=run_id,
            request=message.request,
            settings=message.settings,
            digest=message.digest,
            excluded_counts=excluded,
            tasks=tasks,
            next_seq=len(tasks) + 1,
            updated_at=request.now,
        )
        leg = _Leg(row, request.now, request.free_hosts)
        return self._finish(None, leg, "run_requested")

    @staticmethod
    def _check_bound(settings: ModelCodeEvalSettings) -> None:
        longest = max(settings.try_timeout_seconds, settings.score_timeout_seconds)
        needed = (
            longest + settings.receipt_slack_seconds + settings.nudge_interval_seconds
        )
        if needed >= settings.completion_bound_seconds:
            raise CodeEvalConfigurationError(
                f"timeouts plus slack plus one nudge interval ({needed}s) do not fit "
                f"under the completion bound ({settings.completion_bound_seconds}s)"
            )

    # -- answers --------------------------------------------------------

    def _unit_answered(self, leg: _Leg, msg: ModelUnitAnsweredMessage) -> str:
        seq = self._try_in_flight(leg.row, msg.unit_id)
        if seq is None:
            return "dropped:unit_not_in_flight"
        seq.duration_s += msg.duration_s
        seq.loop_status = msg.loop_status or ""
        if msg.status in ("refused", "infra_error"):
            self._record(leg, seq, _HARNESS)
        elif msg.diff_truncated:
            self._record(leg, seq, _TRUNCATED)
        elif msg.status == "timed_out":
            self._queue_score(leg, seq, msg.diff, timed_out_with_diff=bool(msg.diff))
        elif (
            msg.exit_code == 1
            or msg.loop_status is None
            or msg.loop_status in _LOOP_NOT_RUN
        ):
            self._record(leg, seq, _HARNESS)
        else:
            self._queue_score(leg, seq, msg.diff, timed_out_with_diff=False)
        return "unit_answered"

    def _unit_failed(self, leg: _Leg, unit_id: str) -> str:
        seq = self._try_in_flight(leg.row, unit_id)
        if seq is None:
            return "dropped:unit_not_in_flight"
        self._record(leg, seq, _HARNESS)
        return "unit_failed"

    def _score_answered(self, leg: _Leg, msg: ModelScoreAnsweredMessage) -> str:
        verdict = ModelCodeEvalVerdict(verdict=msg.verdict, source=msg.source)
        if not msg.model_arm:
            task = self._task(leg.row, msg.task_key)
            if (
                task is None
                or task.control_slot is not EnumCodeEvalSlot.SCORE_IN_FLIGHT
            ):
                return "dropped:score_not_in_flight"
            self._record_control(leg, task, verdict)
            return "control_scored"
        seq = next(
            (
                s
                for s in leg.row.sequences
                if s.task_key == msg.task_key
                and s.model == msg.model_arm
                and s.slot is EnumCodeEvalSlot.SCORE_IN_FLIGHT
                and len(s.verdicts) + 1 == msg.try_ordinal
            ),
            None,
        )
        if seq is None:
            return "dropped:score_not_in_flight"
        self._record(leg, seq, verdict)
        return "try_scored"

    def _nudge(self, leg: _Leg) -> str:
        settings = leg.row.settings
        try_limit = settings.try_timeout_seconds + settings.receipt_slack_seconds
        score_limit = settings.score_timeout_seconds + settings.receipt_slack_seconds
        expired = False
        for task in leg.row.tasks:
            if (
                task.control_slot is EnumCodeEvalSlot.SCORE_IN_FLIGHT
                and task.control_sent_at is not None
                and (leg.now - task.control_sent_at).total_seconds() > score_limit
            ):
                self._record_control(leg, task, _HARNESS)
                expired = True
        for seq in leg.row.sequences:
            if seq.sent_at is None:
                continue
            age = (leg.now - seq.sent_at).total_seconds()
            if (seq.slot is EnumCodeEvalSlot.TRY_IN_FLIGHT and age > try_limit) or (
                seq.slot is EnumCodeEvalSlot.SCORE_IN_FLIGHT and age > score_limit
            ):
                self._record(leg, seq, _HARNESS)
                expired = True
        return "nudge_expired" if expired else "nudge"

    # -- verdicts -------------------------------------------------------

    def _record_control(
        self, leg: _Leg, task: ModelCodeEvalTaskRow, verdict: ModelCodeEvalVerdict
    ) -> None:
        task.control = verdict
        task.control_slot = EnumCodeEvalSlot.DONE
        leg.rows.append(
            ModelVerdictRow(
                task_key=task.task_key,
                try_ordinal=0,
                is_control=True,
                verdict=verdict.verdict,
                source=verdict.source,
                band=task.band,
            )
        )
        if verdict.verdict is _PASS:
            for arm in leg.row.request.arms:
                leg.row.sequences.append(
                    ModelCodeEvalSequenceRow(
                        task_key=task.task_key,
                        model=arm.model,
                        backend_id=arm.backend_id,
                    )
                )

    def _record(
        self, leg: _Leg, seq: ModelCodeEvalSequenceRow, verdict: ModelCodeEvalVerdict
    ) -> None:
        seq.verdicts.append(verdict)
        ordinal = len(seq.verdicts)
        leg.rows.append(
            ModelVerdictRow(
                task_key=seq.task_key,
                model_arm=seq.model,
                try_ordinal=ordinal,
                is_control=False,
                verdict=verdict.verdict,
                source=verdict.source,
                band=self._band(leg.row, seq.task_key),
                loop_status=seq.loop_status,
                duration_s=seq.duration_s,
                timed_out_with_diff=seq.timed_out_with_diff,
            )
        )
        seq.unit_id, seq.host, seq.sent_at = "", "", None
        seq.draft_diff, seq.timed_out_with_diff = "", False
        seq.slot = (
            EnumCodeEvalSlot.DONE
            if self._ended(leg.row, seq)
            else EnumCodeEvalSlot.PENDING
        )

    def _queue_score(
        self,
        leg: _Leg,
        seq: ModelCodeEvalSequenceRow,
        diff: str,
        *,
        timed_out_with_diff: bool,
    ) -> None:
        seq.slot = EnumCodeEvalSlot.SCORE_QUEUED
        seq.draft_diff = diff
        seq.timed_out_with_diff = timed_out_with_diff
        seq.queued_seq = leg.row.next_seq
        leg.row.next_seq += 1

    # -- the leg's tail: send, place, close ------------------------------

    def _finish(
        self, stored: ModelCodeEvalRunRow | None, leg: _Leg, transition: str
    ) -> ModelCodeEvalReduceOutput:
        row = leg.row
        self._send_oldest_score(leg)
        self._place(leg)
        self._close_if_rule_holds(leg)
        changed = stored is None or row != stored
        if changed:
            row.updated_at = leg.now
        return ModelCodeEvalReduceOutput(
            row=row if changed else stored,
            transition=transition,
            score_requests=tuple(leg.scores),
            unit_requests=tuple(leg.units),
            verdict_rows=tuple(leg.rows),
            closed=leg.closed,
            row_changed=changed,
        )

    def _send_oldest_score(self, leg: _Leg) -> None:
        row = leg.row
        in_flight = any(
            t.control_slot is EnumCodeEvalSlot.SCORE_IN_FLIGHT for t in row.tasks
        ) or any(s.slot is EnumCodeEvalSlot.SCORE_IN_FLIGHT for s in row.sequences)
        if in_flight:
            return
        queued: list[tuple[int, ModelCodeEvalTaskRow | ModelCodeEvalSequenceRow]] = [
            (t.control_queued_seq, t)
            for t in row.tasks
            if t.control_slot is EnumCodeEvalSlot.SCORE_QUEUED
        ] + [
            (s.queued_seq, s)
            for s in row.sequences
            if s.slot is EnumCodeEvalSlot.SCORE_QUEUED
        ]
        if not queued:
            return
        _, item = min(queued, key=lambda pair: pair[0])
        if isinstance(item, ModelCodeEvalTaskRow):
            item.control_slot = EnumCodeEvalSlot.SCORE_IN_FLIGHT
            item.control_sent_at = leg.now
            leg.scores.append(
                ModelScoreToSend(
                    task_key=item.task_key,
                    context_arm="held",
                    try_ordinal=1,
                )
            )
            return
        item.slot = EnumCodeEvalSlot.SCORE_IN_FLIGHT
        item.sent_at = leg.now
        leg.scores.append(
            ModelScoreToSend(
                task_key=item.task_key,
                model_arm=item.model,
                context_arm="off",
                try_ordinal=len(item.verdicts) + 1,
                draft_diff=item.draft_diff,
                timed_out_with_diff=item.timed_out_with_diff,
            )
        )

    def _place(self, leg: _Leg) -> None:
        for seq in leg.row.sequences:
            if not leg.hosts:
                return
            if seq.slot is not EnumCodeEvalSlot.PENDING:
                continue
            task = self._task(leg.row, seq.task_key)
            if (
                task is None
                or task.control is None
                or task.control.verdict is not _PASS
            ):
                continue
            ordinal = len(seq.verdicts) + 1
            host = leg.hosts.pop(0)
            seq.slot = EnumCodeEvalSlot.TRY_IN_FLIGHT
            seq.unit_id = unit_id_for(leg.row.run_id, seq.task_key, seq.model, ordinal)
            seq.host = host
            seq.sent_at = leg.now
            leg.units.append(
                ModelUnitToPlace(
                    unit_id=seq.unit_id,
                    task_key=seq.task_key,
                    model=seq.model,
                    backend_id=seq.backend_id,
                    try_ordinal=ordinal,
                    host=host,
                )
            )

    def _close_if_rule_holds(self, leg: _Leg) -> None:
        row = leg.row
        if row.closed:
            return
        if any(t.control is None for t in row.tasks):
            return
        if any(s.slot is not EnumCodeEvalSlot.DONE for s in row.sequences):
            return
        row.closed = True
        row.in_flight = False
        row.state = "COMPLETED"
        leg.closed = ModelCodeEvalRunClosed(
            correlation_id=row.run_id,
            tenant_id=row.request.tenant_id,
            harness_revision=row.request.harness_revision,
            publish_cells=row.request.publish_cells,
            attempt_budgets=row.settings.attempt_budgets,
            digest=row.digest,
            closed_at=leg.now,
            abandoned=False,
            excluded_counts=dict(row.excluded_counts),
            controls=tuple(
                ModelCodeEvalControlReading(
                    task_key=t.task_key,
                    verdict=t.control.label if t.control else "",
                    held=bool(t.control and t.control.verdict is _PASS),
                    could_not_be_run=bool(
                        t.control
                        and t.control.verdict is _UNSCORABLE
                        and t.control.source
                        is not EnumCodeScoreSource.FALSIFIER_NOT_HELD
                    ),
                )
                for t in row.tasks
            ),
            sequences=tuple(
                ModelCodeEvalSequenceReading(
                    task_key=s.task_key,
                    model=s.model,
                    backend_id=s.backend_id,
                    band=self._band(row, s.task_key),
                    verdicts=tuple(v.label for v in s.verdicts),
                    duration_s=s.duration_s,
                )
                for s in row.sequences
            ),
        )

    # -- helpers --------------------------------------------------------

    @staticmethod
    def _ended(row: ModelCodeEvalRunRow, seq: ModelCodeEvalSequenceRow) -> bool:
        last_is_pass = bool(seq.verdicts) and seq.verdicts[-1].verdict is _PASS
        return last_is_pass or len(seq.verdicts) >= max(row.settings.attempt_budgets)

    @staticmethod
    def _task(row: ModelCodeEvalRunRow, task_key: str) -> ModelCodeEvalTaskRow | None:
        return next((t for t in row.tasks if t.task_key == task_key), None)

    def _band(self, row: ModelCodeEvalRunRow, task_key: str) -> str:
        task = self._task(row, task_key)
        return task.band if task else ""

    @staticmethod
    def _try_in_flight(
        row: ModelCodeEvalRunRow, unit_id: str
    ) -> ModelCodeEvalSequenceRow | None:
        return next(
            (
                s
                for s in row.sequences
                if s.slot is EnumCodeEvalSlot.TRY_IN_FLIGHT and s.unit_id == unit_id
            ),
            None,
        )

    @staticmethod
    def _dropped(
        stored: ModelCodeEvalRunRow | None, reason: str
    ) -> ModelCodeEvalReduceOutput:
        return ModelCodeEvalReduceOutput(row=stored, transition=reason, dropped=True)


__all__ = ["CodeEvalConfigurationError", "HandlerCodeEvalTrialReducer", "unit_id_for"]
