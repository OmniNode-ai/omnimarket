# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Settle a rubric-failed attempt: swap, shrink, then escalate (OMN-20172, plan 13.6 and 13.10).

The ladder climbs three rungs, each decided by the pure policy node:

1. one configuration swap on the same model and backend, with the next-best
   MET configuration (the caller found it in the capability map),
2. shrink: the split compute cuts the request into lower-band units, each unit
   runs on the same model and backend and is checked by the class rubric, a
   failed unit splits once more (depth cap) or escalates alone, the recombine
   compute joins the answers and the class rubric checks the WHOLE; only a
   whole that passes is accepted,
3. escalate: the original request goes to the next responder in the class chain.

Every call to a model is behind ``ProtocolShrinkRetryPorts``; this handler
holds no model client. The module's attempts are returned as events carrying
their attempt kind, split id and parent attempt id (OMN-20168).
"""

from __future__ import annotations

from typing import Protocol, cast
from uuid import UUID, uuid5

from omnimarket.enums.enum_delegation_attempt_kind import EnumDelegationAttemptKind
from omnimarket.models.delegation.delegation_attempt_lineage import (
    ATTEMPT_ID_NAMESPACE,
    attempt_id_for,
)
from omnimarket.models.delegation.model_shrink_retry import (
    EnumShrinkRetryReason,
    EnumShrinkRetryRung,
    EnumShrinkRetryScope,
    ModelShrinkRetryCaps,
    ModelShrinkRetryDecisionRequest,
)
from omnimarket.models.delegation.model_shrink_retry_outcome import (
    EnumShrinkRetryResolution,
    ModelShrinkRetryOutcome,
    ModelShrinkUnitOutcome,
    ModelShrinkWholeCheck,
)
from omnimarket.models.delegation.wire.model_attempt_configuration import (
    ModelAttemptConfiguration,
)
from omnimarket.models.model_delegation_split_recombine import (
    DelegationTaskClass,
    EnumDelegationSizeBand,
    ModelDelegationRecombineRequest,
    ModelDelegationSplitRequest,
    ModelDelegationSplitSuccess,
    ModelDelegationSplitUnit,
    ModelDelegationUnitAnswer,
)
from omnimarket.nodes.node_delegation_recombine_compute.handlers.handler_delegation_recombine import (
    HandlerDelegationRecombine,
)
from omnimarket.nodes.node_delegation_shrink_retry_compute.handlers.handler_shrink_retry_policy import (
    HandlerShrinkRetryPolicy,
)
from omnimarket.nodes.node_delegation_split_compute.handlers.handler_delegation_split import (
    HandlerDelegationSplit,
)

_DECOMPOSABLE: frozenset[str] = frozenset(
    {"summarization", "code_review", "review", "document", "migration"}
)


class ProtocolShrinkRetryPorts(Protocol):
    """The model calls the shrink path makes, injected by the dispatch."""

    async def run_unit(self, unit: ModelDelegationSplitUnit) -> ModelShrinkUnitOutcome:
        """Run one unit on the same model and backend that failed; gate it at unit level."""
        ...

    async def escalate_unit(
        self, unit: ModelDelegationSplitUnit
    ) -> ModelShrinkUnitOutcome:
        """Send one failed unit alone to the next responder in the class chain."""
        ...

    async def check_whole(self, answer: str) -> ModelShrinkWholeCheck:
        """Check the recombined whole with the class rubric, whole-level criteria included."""
        ...


class _Run:
    """Mutable bookkeeping for one shrink path."""

    def __init__(
        self,
        *,
        correlation_id: UUID,
        base_index: int,
        parent_attempt_id: UUID,
        split_id: UUID,
        unit_cap: int,
    ) -> None:
        self.correlation_id = correlation_id
        self.base_index = base_index
        self.parent_attempt_id = parent_attempt_id
        self.split_id = split_id
        self.unit_cap = unit_cap
        self.events: list[dict[str, object]] = []
        self.unit_attempts = 0
        self.max_depth_reached = 0

    def record(
        self,
        attempt: dict[str, object],
        *,
        kind: EnumDelegationAttemptKind,
        parent: UUID,
    ) -> UUID:
        attempt_id = attempt_id_for(
            self.correlation_id, self.base_index + len(self.events)
        )
        self.events.append(
            {
                **attempt,
                "attempt_id": str(attempt_id),
                "attempt_kind": kind.value,
                "parent_attempt_id": str(parent),
                "split_id": str(self.split_id),
            }
        )
        return attempt_id


class HandlerShrinkRetry:
    """Settles one rubric-failed whole attempt; stateless between calls."""

    def __init__(
        self,
        *,
        policy: HandlerShrinkRetryPolicy | None = None,
        splitter: HandlerDelegationSplit | None = None,
        recombiner: HandlerDelegationRecombine | None = None,
    ) -> None:
        self._policy = policy or HandlerShrinkRetryPolicy()
        self._splitter = splitter or HandlerDelegationSplit()
        self._recombiner = recombiner or HandlerDelegationRecombine()

    def _split(
        self,
        *,
        task_id: str,
        task_class: str,
        size_band: EnumDelegationSizeBand | None,
        source: str,
    ) -> ModelDelegationSplitSuccess | None:
        """The split compute's units, or None when the source cannot shrink."""
        if size_band is None or task_class not in _DECOMPOSABLE:
            return None
        if task_class == "migration":
            return None  # migration units are caller-listed, never cut from prose
        result = self._splitter.handle(
            ModelDelegationSplitRequest(
                task_id=task_id,
                task_class=_as_task_class(task_class),
                size_band=size_band,
                source=source,
            )
        )
        return result if isinstance(result, ModelDelegationSplitSuccess) else None

    async def settle(
        self,
        *,
        task_id: str,
        task_class: str,
        size_band: EnumDelegationSizeBand | None,
        source: str,
        caps: ModelShrinkRetryCaps | None,
        config_swaps_used: int,
        second_configuration: ModelAttemptConfiguration | None,
        ports: ProtocolShrinkRetryPorts,
        correlation_id: UUID,
        failed_attempt_index: int,
    ) -> ModelShrinkRetryOutcome:
        """Decide and, when the decision is to shrink, run the whole shrink path.

        ``failed_attempt_index`` is the ladder index of the rubric-failed whole
        attempt, which is the parent of every attempt this path records. The events
        returned take the indexes after it, so the caller appends them in order.
        """
        first = self._split(
            task_id=task_id, task_class=task_class, size_band=size_band, source=source
        )
        decision = self._policy.handle(
            ModelShrinkRetryDecisionRequest(
                task_id=task_id,
                task_class=task_class,
                scope=EnumShrinkRetryScope.WHOLE,
                caps=caps,
                config_swaps_used=config_swaps_used,
                split_depth=0,
                splittable=first is not None,
                second_configuration=second_configuration,
            )
        )
        if decision.rung is EnumShrinkRetryRung.CONFIG_SWAP:
            return ModelShrinkRetryOutcome(
                resolution=EnumShrinkRetryResolution.CONFIG_SWAP,
                reason=decision.reason,
                configuration=decision.configuration,
            )
        if decision.rung is not EnumShrinkRetryRung.SHRINK:
            return ModelShrinkRetryOutcome(
                resolution=EnumShrinkRetryResolution.ESCALATE, reason=decision.reason
            )
        assert first is not None
        assert caps is not None
        parent = attempt_id_for(correlation_id, failed_attempt_index)
        run = _Run(
            correlation_id=correlation_id,
            base_index=failed_attempt_index + 1,
            parent_attempt_id=parent,
            split_id=uuid5(ATTEMPT_ID_NAMESPACE, f"{correlation_id}:split:{task_id}"),
            unit_cap=caps.unit_attempts_per_unit * len(first.units),
        )
        answers = await self._solve_level(
            first.units,
            depth=1,
            parent=parent,
            run=run,
            ports=ports,
            caps=caps,
            task_class=task_class,
            initial_units=len(first.units),
        )
        whole = self._recombiner.handle(
            ModelDelegationRecombineRequest(
                task_id=task_id,
                task_class=_as_task_class(task_class),
                answers=tuple(answers),
            )
        )
        check = await ports.check_whole(whole.answer)
        run.record(
            check.attempt,
            kind=EnumDelegationAttemptKind.RECOMBINE_CHECK,
            parent=parent,
        )
        if check.passed:
            return ModelShrinkRetryOutcome(
                resolution=EnumShrinkRetryResolution.ACCEPTED,
                reason=EnumShrinkRetryReason.DECOMPOSABLE,
                answer=whole.answer,
                findings=whole.findings,
                events=tuple(run.events),
                unit_attempts=run.unit_attempts,
                split_depth=run.max_depth_reached,
            )
        escalate = self._policy.handle(
            ModelShrinkRetryDecisionRequest(
                task_id=task_id,
                task_class=task_class,
                scope=EnumShrinkRetryScope.RECOMBINED,
                caps=caps,
            )
        )
        return ModelShrinkRetryOutcome(
            resolution=EnumShrinkRetryResolution.ESCALATE,
            reason=escalate.reason,
            events=tuple(run.events),
            unit_attempts=run.unit_attempts,
            split_depth=run.max_depth_reached,
        )

    async def _solve_level(
        self,
        units: tuple[ModelDelegationSplitUnit, ...],
        *,
        depth: int,
        parent: UUID,
        run: _Run,
        ports: ProtocolShrinkRetryPorts,
        caps: ModelShrinkRetryCaps,
        task_class: str,
        initial_units: int,
    ) -> tuple[ModelDelegationUnitAnswer, ...]:
        """Every unit of one level: all first attempts, then what each failure does.

        The first attempts of the whole level are counted before any failed unit
        asks to split, so the unit-attempt cap holds for the units still to run.
        """
        run.max_depth_reached = max(run.max_depth_reached, depth)
        ran: list[tuple[ModelDelegationSplitUnit, ModelShrinkUnitOutcome, UUID]] = []
        for unit in units:
            outcome = await ports.run_unit(unit)
            run.unit_attempts += 1
            attempt_id = run.record(
                outcome.attempt,
                kind=EnumDelegationAttemptKind.SPLIT_CHILD,
                parent=parent,
            )
            ran.append((unit, outcome, attempt_id))
        answers: list[ModelDelegationUnitAnswer] = []
        for unit, outcome, attempt_id in ran:
            if outcome.passed:
                answers.append(_answer(unit, outcome))
                continue
            children = self._split(
                task_id=unit.unit_id,
                task_class=task_class,
                size_band=unit.size_band,
                source=unit.source,
            )
            decision = self._policy.handle(
                ModelShrinkRetryDecisionRequest(
                    task_id=unit.unit_id,
                    task_class=task_class,
                    scope=EnumShrinkRetryScope.UNIT,
                    caps=caps,
                    split_depth=depth,
                    splittable=children is not None,
                    unit_count=initial_units,
                    unit_attempts_used=run.unit_attempts,
                )
            )
            if (
                decision.rung is EnumShrinkRetryRung.RESPLIT_UNIT
                and children is not None
                and run.unit_attempts + len(children.units) <= run.unit_cap
            ):
                child_answers = await self._solve_level(
                    children.units,
                    depth=depth + 1,
                    parent=attempt_id,
                    run=run,
                    ports=ports,
                    caps=caps,
                    task_class=task_class,
                    initial_units=initial_units,
                )
                joined = self._recombiner.handle(
                    ModelDelegationRecombineRequest(
                        task_id=unit.unit_id,
                        task_class=_as_task_class(task_class),
                        answers=child_answers,
                    )
                )
                answers.append(
                    ModelDelegationUnitAnswer(
                        unit_id=unit.unit_id,
                        position=unit.position,
                        answer=joined.answer,
                        findings=joined.findings,
                    )
                )
                continue
            escalated = await ports.escalate_unit(unit)
            run.record(
                escalated.attempt,
                kind=EnumDelegationAttemptKind.UNIT_ESCALATION,
                parent=attempt_id,
            )
            answers.append(_answer(unit, escalated))
        return tuple(answers)


def _answer(
    unit: ModelDelegationSplitUnit, outcome: ModelShrinkUnitOutcome
) -> ModelDelegationUnitAnswer:
    return ModelDelegationUnitAnswer(
        unit_id=unit.unit_id,
        position=unit.position,
        answer=outcome.answer,
        findings=outcome.findings,
    )


def _as_task_class(task_class: str) -> DelegationTaskClass:
    """The split models' closed class literal, refusing a class they do not name."""
    if task_class not in _DECOMPOSABLE and task_class not in {
        "planning",
        "reasoning",
        "code_generation",
    }:
        raise ValueError(f"task class {task_class!r} has no split rule")
    return cast(DelegationTaskClass, task_class)


__all__ = ["HandlerShrinkRetry", "ProtocolShrinkRetryPorts"]
