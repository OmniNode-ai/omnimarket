# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Definition-B run operation of the delegation evals plan (OMN-19793, EV.4).

Reads one manifest's labelled items through an injected tenant-bound port,
replays them through the gate-eval compute, and returns exactly one terminal
payload: a run-completed event with status ``completed`` or ``failed``. It never
publishes; the runtime does. The run also scores each rubric class's verdict
(rubric arm) against the labels, with the class lines from
``configs/delegation_class_rubrics.v1.yaml``.

The eval run id is ``uuid5`` of the manifest id, the sha256 of the ordered label
set (item key and label, under one rater role and rubric version) and the gate
version, plus the class rubric version when a rubric class occurs in the run.
It never depends on the delivery, so a redelivered command yields the same run
id and the same rows, and a gate or class rubric change is a new run. The TLA+
model ``formal/delegation_eval_run/DelegationEvalRun.tla`` checks that design
and fails on each of its three mutations.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID, uuid5

from omnimarket.delegation.rubric.attempt_verdict import record_attempt_rubric_verdict
from omnimarket.delegation.rubric.contract_loader import load_delegation_class_rubrics
from omnimarket.events.delegation_eval import (
    ModelDelegationEvalItemVerdict,
    ModelDelegationEvalResultRow,
    ModelDelegationEvalRunCompleted,
    ModelDelegationEvalRunRequest,
)
from omnimarket.events.delegation_gate_eval.enum_gate_eval_run_status import (
    EnumGateEvalRunStatus,
)
from omnimarket.events.delegation_gate_eval.model_delegation_gate_eval_request import (
    ModelDelegationGateEvalRequest,
)
from omnimarket.events.delegation_gate_eval.model_gate_eval_item import (
    ModelGateEvalItem,
)
from omnimarket.events.delegation_gate_eval.model_gate_rate_row import (
    ModelGateRateRow,
)
from omnimarket.models.delegation.wire.model_attempt_rubric_verdict import (
    ModelAttemptRubricVerdict,
)
from omnimarket.models.ranges import ModelRangeAcceptanceLine
from omnimarket.nodes.node_delegation_eval_run_orchestrator.models.model_eval_run import (
    ModelEvalRunResult,
)
from omnimarket.nodes.node_delegation_eval_run_orchestrator.protocols.protocol_delegation_eval_labelled_items import (
    ProtocolDelegationEvalLabelledItems,
)
from omnimarket.nodes.node_delegation_gate_eval_compute.handlers.handler_delegation_gate_eval import (
    HandlerDelegationGateEval,
)

#: Fixed namespace for eval run ids, so the same inputs give the same id on any host.
EVAL_RUN_NAMESPACE = UUID("5b0c7e2a-19f3-5d4e-9a93-6c1d0e4f1979")


class _ClassRubricContract(Protocol):
    """The shared loader's contract surface, without importing node models."""

    @property
    def rubric_version(self) -> str: ...

    @property
    def classes(self) -> Mapping[str, object]: ...

    @property
    def false_pass_lines(self) -> Mapping[str, ModelRangeAcceptanceLine]: ...

    @property
    def false_refusal_lines(self) -> Mapping[str, ModelRangeAcceptanceLine]: ...


def label_set_sha256(
    items: tuple[ModelGateEvalItem, ...], *, rater_role: str, rubric_version: str
) -> str:
    """sha256 of the ordered (item key, label) set under one rater and rubric."""
    ordered = sorted((item.item_id, str(item.label)) for item in items)
    body = json.dumps(
        {"rater_role": rater_role, "rubric_version": rubric_version, "items": ordered},
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(body.encode()).hexdigest()


def eval_run_id(
    manifest_id: str,
    label_sha: str,
    gate_version: str,
    class_rubric_version: str | None = None,
) -> UUID:
    """Stable uuid5, extended by the class rubric version for rubric runs."""
    name = f"{manifest_id}\n{label_sha}\n{gate_version}"
    if class_rubric_version is not None:
        name += f"\n{class_rubric_version}"
    return uuid5(EVAL_RUN_NAMESPACE, name)


def _error_bound(lower_bound: float | None) -> float | None:
    return None if lower_bound is None else 1.0 - lower_bound


def _result_row(row: ModelGateRateRow) -> ModelDelegationEvalResultRow:
    fp, fr = row.false_pass_evaluation, row.false_refusal_evaluation
    return ModelDelegationEvalResultRow(
        task_class=row.task_class,
        stratum=row.stratum,
        arm=row.arm,
        total_n=row.total_n,
        accepted_n=row.accepted_n,
        false_pass_count=row.false_pass_count,
        false_pass_rate=row.false_pass_count / row.accepted_n
        if row.accepted_n
        else None,
        false_pass_upper_bound=_error_bound(fp.lower_bound),
        false_pass_wilson_low=row.false_pass_wilson.low,
        false_pass_wilson_high=row.false_pass_wilson.high,
        false_pass_line_verdict=str(fp.verdict),
        false_pass_required_n=fp.required_n,
        refused_n=row.refused_n,
        false_refusal_count=row.false_refusal_count,
        false_refusal_rate=row.false_refusal_count / row.refused_n
        if row.refused_n
        else None,
        false_refusal_upper_bound=_error_bound(fr.lower_bound),
        false_refusal_wilson_low=row.false_refusal_wilson.low,
        false_refusal_wilson_high=row.false_refusal_wilson.high,
        false_refusal_line_verdict=str(fr.verdict),
        undetermined_n=row.undetermined_n,
        undetermined_share=row.undetermined_share,
    )


class HandlerDelegationEvalRun:
    """One command in, one terminal payload out; no transport, no workflow state."""

    def __init__(
        self,
        items_source: ProtocolDelegationEvalLabelledItems | None = None,
        gate_eval: HandlerDelegationGateEval | None = None,
        clock: Callable[[], datetime] | None = None,
        *,
        class_rubrics: Callable[[], _ClassRubricContract] | None = None,
        rubric_verdict: Callable[..., ModelAttemptRubricVerdict] | None = None,
    ) -> None:
        self._items_source = items_source
        self._gate_eval = gate_eval or HandlerDelegationGateEval()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._class_rubrics = (
            load_delegation_class_rubrics if class_rubrics is None else class_rubrics
        )
        self._rubric_verdict = (
            record_attempt_rubric_verdict if rubric_verdict is None else rubric_verdict
        )

    def handle(self, request: ModelDelegationEvalRunRequest) -> ModelEvalRunResult:
        if self._items_source is None:
            raise RuntimeError("tenant-scoped labelled-item source is required")
        items = tuple(
            sorted(
                self._items_source.get_labelled_items(
                    request.manifest_id, request.rater_role, request.rubric_version
                ),
                key=lambda item: item.item_id,
            )
        )
        sha = label_set_sha256(
            items, rater_role=request.rater_role, rubric_version=request.rubric_version
        )
        run_id = eval_run_id(request.manifest_id, sha, request.gate_version)
        common = {
            "tenant_id": request.tenant_id,
            "eval_run_id": run_id,
            "manifest_id": request.manifest_id,
            "label_set_sha256": sha,
            "gate_version": request.gate_version,
            "rater_role": request.rater_role,
            "rubric_version": request.rubric_version,
            "observed_at": self._clock(),
        }
        if not items:
            return ModelEvalRunResult(
                payload=ModelDelegationEvalRunCompleted(
                    **common,
                    status=str(EnumGateEvalRunStatus.FAILED),
                    failure_reasons=(
                        f"no labelled items for manifest {request.manifest_id} "
                        f"under rater {request.rater_role} rubric {request.rubric_version}",
                    ),
                )
            )
        try:
            contract = self._class_rubrics()
        except Exception as exc:
            return ModelEvalRunResult(
                payload=ModelDelegationEvalRunCompleted(
                    **common,
                    status=str(EnumGateEvalRunStatus.FAILED),
                    failure_reasons=(
                        f"class rubric contract unavailable: {type(exc).__name__}",
                    ),
                )
            )
        rubric_classes = {item.task_class for item in items} & contract.classes.keys()
        run_id = eval_run_id(
            request.manifest_id,
            sha,
            request.gate_version,
            contract.rubric_version if rubric_classes else None,
        )
        common["eval_run_id"] = run_id
        items = tuple(
            item.model_copy(
                update={
                    "rubric_verdict": self._rubric_verdict(
                        task_class=item.task_class,
                        request_text=item.prompt_text,
                        answer_text=item.recorded_answer,
                    )
                }
            )
            if item.task_class in rubric_classes and item.recorded_answer is not None
            else item
            for item in items
        )
        result = self._gate_eval.handle(
            ModelDelegationGateEvalRequest(
                run_id=str(run_id),
                items=items,
                rubric_false_pass_lines={
                    task_class: line
                    for task_class, line in contract.false_pass_lines.items()
                    if task_class in rubric_classes
                },
                rubric_false_refusal_lines={
                    task_class: line
                    for task_class, line in contract.false_refusal_lines.items()
                    if task_class in rubric_classes
                },
            )
        )
        if result.status != EnumGateEvalRunStatus.COMPLETED:
            return ModelEvalRunResult(
                payload=ModelDelegationEvalRunCompleted(
                    **common,
                    status=str(result.status),
                    failure_reasons=result.failure_reasons,
                )
            )
        return ModelEvalRunResult(
            payload=ModelDelegationEvalRunCompleted(
                **common,
                status=str(result.status),
                item_verdicts=tuple(
                    ModelDelegationEvalItemVerdict(
                        item_key=verdict.item_id,
                        task_class=verdict.task_class,
                        stratum=verdict.stratum,
                        label=str(verdict.label),
                        recorded_verdict=None
                        if verdict.recorded_verdict is None
                        else str(verdict.recorded_verdict),
                        recorded_deciding_check=verdict.recorded_deciding_check,
                        replayed_verdict=str(verdict.replayed.verdict),
                        replayed_deciding_check=verdict.replayed.deciding_check,
                        replay_count=verdict.replay_count,
                    )
                    for verdict in result.item_verdicts
                ),
                results=tuple(_result_row(row) for row in result.rate_rows),
            )
        )


__all__ = [
    "EVAL_RUN_NAMESPACE",
    "HandlerDelegationEvalRun",
    "eval_run_id",
    "label_set_sha256",
]
