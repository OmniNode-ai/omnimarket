# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Turn one full read's line outcomes into the integration readings the integration projection holds. Pure.

* SEEN_RUNNING: ``running``, with the entry point that runs the change (its first node, its unit, or the overlay
  store checkout) and one line of the reading (verdict, kind, in build, last run time and last result).
* NOT_RUNNING: the state and gap class the contract maps the reader's missing step to, with the reader's next action
  as the one step.
* UNREAD, or a line whose activity failed every attempt: ``unproven``, ``needs_reading``, with the cause as the step.
  A library change in the running build is ``library``: it runs nowhere by design.

Every reading carries the time it was read.
"""

from __future__ import annotations

from omnimarket.models.integration_reading import (
    EnumIntegrationGapClass,
    EnumIntegrationState,
    ModelIntegrationReading,
    ModelIntegrationReadingBatch,
)
from omnimarket.models.model_merged_change_reading import ModelMcrReading
from omnimarket.nodes.node_merged_change_runner_read_workflow.models.model_runner_read import (
    ModelMergeFacts,
    ModelRunnerIntegrationConfig,
    ModelRunnerIntegrationRequest,
    ModelRunnerIntegrationResult,
    ModelRunnerLineOutcome,
    ModelRunnerReadCounts,
)


def _running_line(reading: ModelMcrReading) -> str:
    last_run = reading.last_run_at.isoformat() if reading.last_run_at else "none"
    return (
        f"{reading.verdict} kind={reading.kind} in_build={reading.in_build}"
        f" last_run_at={last_run} last_result={reading.last_result or 'none'}"
    )


def _running_path(
    reading: ModelMcrReading, unit: str, cfg: ModelRunnerIntegrationConfig
) -> str:
    if unit:
        return unit
    if reading.nodes:
        return reading.nodes[0].node
    return cfg.store_running_path


class HandlerRunnerReadingIntegration:
    """``handle(ModelRunnerIntegrationRequest) -> ModelRunnerIntegrationResult``."""

    def handle(
        self, request: ModelRunnerIntegrationRequest
    ) -> ModelRunnerIntegrationResult:
        cfg = request.config
        readings = [self._one(o, request, cfg) for o in request.outcomes]
        verdicts = [o.reading.verdict if o.reading else "" for o in request.outcomes]
        running = verdicts.count(cfg.verdict_running)
        not_running = verdicts.count(cfg.verdict_not_running)
        unread = len(verdicts) - running - not_running
        batch = ModelIntegrationReadingBatch(
            as_of=request.as_of, origin=request.origin, readings=tuple(readings)
        )
        return ModelRunnerIntegrationResult(
            batch=batch,
            counts=ModelRunnerReadCounts(
                declared=len(request.outcomes),
                seen_running=running,
                not_running=not_running,
                unread=unread,
            ),
        )

    @staticmethod
    def _one(
        outcome: ModelRunnerLineOutcome,
        request: ModelRunnerIntegrationRequest,
        cfg: ModelRunnerIntegrationConfig,
    ) -> ModelIntegrationReading:
        facts = request.merge_facts.get(outcome.key) or ModelMergeFacts()
        unit_target = request.unit_targets.get(outcome.key, "")
        reading = outcome.reading
        common = {
            "repository": outcome.repo,
            "number": outcome.number,
            "title": facts.title or f"{outcome.key} (merge commit unread)",
            "merged_at": facts.merged_at or request.as_of,
            "target": unit_target or cfg.target_runtime,
            "read_at": outcome.read_at,
            "origin": request.origin,
        }
        if reading is None:
            return ModelIntegrationReading(
                **common,
                state=EnumIntegrationState.UNPROVEN,
                gap_class=EnumIntegrationGapClass.NEEDS_READING,
                gap="unread",
                step=f"unread: {outcome.unread_cause or 'the reader answered nothing'}",
            )
        unit = unit_target.split(" ", 1)[0] if unit_target else ""
        line = _running_line(reading)
        if reading.verdict == cfg.verdict_running:
            return ModelIntegrationReading(
                **common,
                state=EnumIntegrationState.RUNNING,
                gap_class=EnumIntegrationGapClass.NONE,
                running_path=_running_path(reading, unit, cfg),
                running_line=line,
            )
        if reading.verdict == cfg.verdict_not_running:
            rule = cfg.missing_steps.get(reading.missing_step) or (
                cfg.unknown_missing_step
            )
            return ModelIntegrationReading(
                **common,
                state=rule.state,
                gap_class=rule.gap_class,
                gap=f"{reading.missing_step or 'not_running'}: {reading.detail}".strip(),
                step=reading.next_action or reading.missing_step or line,
                running_line=line,
            )
        if reading.kind == cfg.library_kind and reading.in_build is True:
            return ModelIntegrationReading(
                **common,
                state=EnumIntegrationState.LIBRARY,
                gap_class=EnumIntegrationGapClass.NONE,
                running_line=line,
            )
        return ModelIntegrationReading(
            **common,
            state=EnumIntegrationState.UNPROVEN,
            gap_class=EnumIntegrationGapClass.NEEDS_READING,
            gap="unread",
            step=f"{reading.verdict}: {reading.detail or 'no detail'}",
            running_line=line,
        )


__all__ = ["HandlerRunnerReadingIntegration"]
