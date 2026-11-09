# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""J8 live run (OMN-23087): one real head, piece 8's chain, then piece 9's answer, under one correlation id.

Canonical shape: ``handle(ModelJoinJ8LiveCommand) -> ModelJoinJ8LiveResult``. A run takes one real pull request head the
collector read, writes piece 8's chain (the node that reads the lab-bound check run posted on the head: a start, then a
success when the current attempt's check passed, or a typed failure when it is missing, superseded or failing), then hands the
head to the landing decision's J8 handler under the same correlation id, which answers on piece 9's chain. The floor's own
receipt fold reads the pair from the journal: a head the decision merges is the join's golden, a head the lab check holds is
its error.

``want`` picks the end of the join to exercise from the real heads the collector holds. A check still running inside its wait
bound is judged by neither end: nothing is emitted.

The two named injected breaks are the join's failing controls. ``blind-decision``: the decision is handed a passing check the
poster never saw, so piece 9 starts after piece 8 failed. ``silent-landing``: piece 8 fails and piece 9 never answers. Both need
a head whose check does not pass, since a passing head has nothing to hide.
Everything it calls is injected: the collector, the emission sink and the clock.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal

from omnimarket.handlers.emission_sink import ChainEmitter, ProtocolEmissionSink
from omnimarket.nodes.node_pr_landing_decision_compute.adapters.watcher_lab_check import (
    ProtocolJ8Subjects,
)
from omnimarket.nodes.node_pr_landing_decision_compute.handlers.handler_landing_join_j8 import (
    HandlerLandingJoinJ8,
)
from omnimarket.nodes.node_pr_landing_decision_compute.handlers.handler_pr_landing_decision import (
    decide_landing,
)
from omnimarket.nodes.node_pr_landing_decision_compute.lab_check import (
    read_lab_check,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.enum_landing import (
    EnumLandingActionKind,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_join_j8 import (
    ModelJoinJ8Policy,
    ModelJoinJ8Request,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_join_j8_live import (
    EnumJ8Break,
    EnumJ8LiveStatus,
    EnumJ8Want,
    ModelJ8Subject,
    ModelJoinJ8LiveCommand,
    ModelJoinJ8LiveOverlay,
    ModelJoinJ8LiveResult,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_lab_check import (
    EnumLabCheckVerdict,
)

_HOLDS = frozenset(
    {
        EnumLabCheckVerdict.MISSING,
        EnumLabCheckVerdict.SUPERSEDED,
        EnumLabCheckVerdict.FAILED,
    }
)


class HandlerLandingJoinJ8Live:
    def __init__(
        self,
        *,
        subjects: ProtocolJ8Subjects,
        sink: ProtocolEmissionSink,
        overlay: ModelJoinJ8LiveOverlay,
        policy: ModelJoinJ8Policy,
        clock: Callable[[], datetime] | None = None,
        backend: Literal["in_memory", "real"] = "real",
    ) -> None:
        self._subjects = subjects
        self._sink = sink
        self._overlay = overlay
        self._policy = policy
        self._clock = clock or (lambda: datetime.now(UTC))
        self._backend = backend

    def handle(self, command: ModelJoinJ8LiveCommand) -> ModelJoinJ8LiveResult:
        cid = command.correlation_id
        if command.inject is not None and not self._overlay.allow_injected_break:
            return self._result(
                EnumJ8LiveStatus.FAILED,
                command,
                detail="an injected break runs only under an overlay that allows it",
            )
        want = command.want if command.inject is None else EnumJ8Want.ERROR
        subject = self._pick(self._subjects.subjects(command.pr), want)
        if subject is None:
            return self._result(
                EnumJ8LiveStatus.NO_SUBJECT,
                command,
                detail=f"no real head among the candidates serves the {want.value} end of the join",
            )
        reading = read_lab_check(
            subject.poster_facts.prs[0],
            subject.poster_facts.observed_at,
            subject.poster_facts.policy,
        )
        if reading is None:
            return self._result(
                EnumJ8LiveStatus.FAILED, command, subject, detail="no lab_check fact"
            )
        if reading.verdict is EnumLabCheckVerdict.PENDING:
            return self._result(
                EnumJ8LiveStatus.PENDING, command, subject, detail=reading.detail
            )

        poster = ChainEmitter(
            self._sink,
            node=self._overlay.poster_node,
            piece=self._policy.from_piece,
            clock=self._clock,
            correlation_id=cid,
            backend=self._backend,
            drill=self._overlay.drill,
        )
        poster.start()
        poster_ended: str
        if reading.verdict is EnumLabCheckVerdict.PASS:
            poster.success({"check_runs_posted": 1})
            poster_ended = "success"
        else:
            poster.failure(self._overlay.poster_failure_code, reading.detail)
            poster_ended = f"failure:{self._overlay.poster_failure_code}"

        if command.inject is EnumJ8Break.SILENT_LANDING:
            return self._result(
                EnumJ8LiveStatus.COMPLETED,
                command,
                subject,
                poster_ended=poster_ended,
                detail="piece 9 was made silent by the injected break",
            )
        answer = HandlerLandingJoinJ8(
            sink=self._sink,
            policy=self._policy,
            clock=self._clock,
            backend=self._backend,
        ).handle(
            ModelJoinJ8Request(correlation_id=cid, pr=subject.pr, facts=subject.facts)
        )
        return self._result(
            EnumJ8LiveStatus.COMPLETED,
            command,
            subject,
            poster_ended=poster_ended,
            outcome=answer,
            detail=answer.detail,
        )

    def _pick(
        self, candidates: tuple[ModelJ8Subject, ...], want: EnumJ8Want
    ) -> ModelJ8Subject | None:
        for subject in candidates:
            if want is EnumJ8Want.ANY:
                return subject
            if want is EnumJ8Want.ERROR and subject.verdict in _HOLDS:
                return subject
            if want is EnumJ8Want.GOLDEN and self._merges(subject):
                return subject
        return None

    @staticmethod
    def _merges(subject: ModelJ8Subject) -> bool:
        if subject.verdict is not EnumLabCheckVerdict.PASS:
            return False
        pr = subject.facts.prs[0]
        return any(
            a.kind is EnumLandingActionKind.MERGE
            and a.subject == subject.pr
            and a.head_sha == pr.head_sha
            for a in decide_landing(subject.facts).actions
        )

    def _result(
        self,
        status: EnumJ8LiveStatus,
        command: ModelJoinJ8LiveCommand,
        subject: ModelJ8Subject | None = None,
        *,
        poster_ended: str | None = None,
        outcome: object | None = None,
        detail: str = "",
    ) -> ModelJoinJ8LiveResult:
        return ModelJoinJ8LiveResult(
            status=status,
            correlation_id=command.correlation_id,
            pr=None if subject is None else subject.pr,
            head_sha=None if subject is None else subject.head_sha,
            verdict=None if subject is None else subject.verdict,
            outcome=getattr(outcome, "outcome", None),
            poster_ended=poster_ended,
            injected=command.inject,
            detail=detail,
        )


__all__ = ["HandlerLandingJoinJ8Live"]
