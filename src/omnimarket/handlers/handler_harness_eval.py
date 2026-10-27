# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Offline checker observations folded through the existing delegation eval run.

Execution belongs to the operator's lab runner. This module never invokes a
harness, model, shell or transport. The gate replays recorded checker facts,
three times as usual; it does not run the subject model three times.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from datetime import datetime
from statistics import median
from uuid import NAMESPACE_URL, UUID, uuid5

from omnimarket.events.delegation_eval import (
    ModelDelegationEvalRunCompleted,
    ModelDelegationEvalRunRequest,
)
from omnimarket.events.delegation_gate_eval.enum_gate_eval_label import (
    EnumGateEvalLabel,
)
from omnimarket.events.delegation_gate_eval.enum_gate_verdict import EnumGateVerdict
from omnimarket.events.delegation_gate_eval.model_gate_eval_item import (
    ModelGateEvalItem,
)
from omnimarket.events.delegation_gate_eval.model_gate_execution_result import (
    ModelGateExecutionResult,
)
from omnimarket.events.delegation_gate_eval.model_gate_replay_verdict import (
    ModelGateReplayVerdict,
)
from omnimarket.models.delegation.model_harness_observation import (
    ModelHarnessObservation,
)
from omnimarket.models.delegation.model_size_band import EnumSizeBand
from omnimarket.models.delegation.wire.model_attempt_configuration import (
    ModelAttemptConfiguration,
)
from omnimarket.models.delegation_capability_map.enum_capability_source import (
    EnumCapabilitySource,
)
from omnimarket.models.delegation_capability_map.model_capability_configuration import (
    ModelCapabilityConfiguration,
)
from omnimarket.models.delegation_capability_map.model_capability_identity import (
    ModelCapabilityIdentity,
)
from omnimarket.models.delegation_capability_map.model_capability_map_run_recorded import (
    ModelCapabilityMapRunRecorded,
)
from omnimarket.models.delegation_capability_map.model_capability_serving_assignment import (
    ModelCapabilityServingAssignment,
)
from omnimarket.models.delegation_capability_map.model_capability_verdict_row import (
    ModelCapabilityVerdictRow,
)
from omnimarket.nodes.node_delegation_eval_run_orchestrator.handlers.handler_delegation_eval_run import (
    HandlerDelegationEvalRun,
)
from omnimarket.nodes.node_delegation_gate_eval_compute.handlers.handler_delegation_gate_eval import (
    HandlerDelegationGateEval,
)


class _Items:
    def __init__(self, items: tuple[ModelGateEvalItem, ...]) -> None:
        self.items = items

    def get_labelled_items(
        self, manifest_id: str, rater_role: str, rubric_version: str
    ) -> tuple[ModelGateEvalItem, ...]:
        return self.items


def _checker_gate(item: ModelGateEvalItem) -> ModelGateReplayVerdict:
    verdict = EnumGateVerdict.UNDETERMINED
    if item.execution_result is not None:
        verdict = (
            EnumGateVerdict.ACCEPTED
            if item.execution_result.passed
            else EnumGateVerdict.REFUSED
        )
    return ModelGateReplayVerdict(
        verdict=verdict, deciding_check="deterministic-checker"
    )


def build_harness_run(
    observations: tuple[ModelHarnessObservation, ...],
    *,
    tenant_id: UUID,
    manifest_id: str,
    observed_at: datetime,
) -> ModelDelegationEvalRunCompleted:
    """Use the same verdicts, class ranges and terminal payload as model evals.

    A successful process claiming an incorrect answer is a recorded false pass.
    The replayed arm uses only the checker. Unavailable items are unlabelable;
    the existing fold excludes them from false-pass/refusal populations.
    """
    identities = {(r.harness, r.model, r.configuration) for r in observations}
    if len(identities) != 1:
        raise ValueError("one harness/model/configuration per run is required")
    if len({r.item_key for r in observations}) != len(observations):
        raise ValueError("duplicate item observations")
    harness, model, configuration = next(iter(identities))
    items = tuple(
        ModelGateEvalItem(
            item_id=row.item_key,
            task_class=row.task_class,
            stratum="fixed-fixture-v1",
            label=(
                EnumGateEvalLabel.ADEQUATE
                if row.passed
                else EnumGateEvalLabel.INADEQUATE
            )
            if row.available
            else EnumGateEvalLabel.UNLABELABLE,
            prompt_text="deterministic fixture checker",
            recorded_verdict=(
                EnumGateVerdict.ACCEPTED
                if row.harness_succeeded
                else EnumGateVerdict.REFUSED
            )
            if row.available
            else None,
            recorded_deciding_check="harness-exit",
            requires_execution=True,
            execution_result=ModelGateExecutionResult(passed=row.passed)
            if row.available
            else None,
        )
        for row in observations
    )
    run = (
        HandlerDelegationEvalRun(
            items_source=_Items(items),
            gate_eval=HandlerDelegationGateEval(gate=_checker_gate),
            clock=lambda: observed_at,
        )
        .handle(
            ModelDelegationEvalRunRequest(
                tenant_id=tenant_id,
                manifest_id=manifest_id,
                rater_role="deterministic-checker",
                rubric_version="harness-checks-v1",
                gate_version="harness-checks-v1",
                harness=harness,
                model=model,
                configuration=configuration,
            )
        )
        .payload
    )
    by_key = {r.item_key: r for r in observations}
    return run.model_copy(
        update={
            "item_verdicts": tuple(
                row.model_copy(
                    update={
                        "duration_s": by_key[row.item_key].duration_s,
                        "failure_kind": by_key[row.item_key].failure_kind,
                    }
                )
                for row in run.item_verdicts
            )
        }
    )


def summarize(
    observations: tuple[ModelHarnessObservation, ...],
) -> list[dict[str, object]]:
    """Descriptive cells only: three trials cannot establish a capability line."""
    groups: dict[tuple[str, str, str, str], list[ModelHarnessObservation]] = (
        defaultdict(list)
    )
    for row in observations:
        groups[(row.harness, row.model, row.configuration, row.task_class)].append(row)
    cells: list[dict[str, object]] = []
    for (harness, model, configuration, task_class), rows in sorted(groups.items()):
        available = [r for r in rows if r.available]
        passes = sum(r.passed for r in available)
        cells.append(
            {
                "harness": harness,
                "model": model,
                "configuration": configuration,
                "task_class": task_class,
                "n": len(available),
                "passes": passes,
                "unavailable": len(rows) - len(available),
                "pass_rate": passes / len(available) if available else None,
                "median_minutes": median(r.duration_s for r in available) / 60
                if available
                else None,
                "failure_kinds": dict(
                    Counter(
                        r.failure_kind or "checker-failed" for r in rows if not r.passed
                    )
                ),
            }
        )
    return cells


def build_harness_capability_run(
    observations: tuple[ModelHarnessObservation, ...],
    *,
    tenant_id: UUID,
    observed_at: datetime,
    suite_revision: str,
    harness_revision: str,
) -> ModelCapabilityMapRunRecorded:
    """Adapt small fixed-fixture trials to the existing capability projection.

    The backend identity includes the harness and complete configuration label,
    so two harnesses using the same model never pool. Unavailable trials create
    no cell. The existing sample-size line still refuses three-item cells.
    """
    rows = []
    assignments = {}
    for row in observations:
        if not row.available:
            continue
        configuration = ModelCapabilityConfiguration(
            parameters=ModelAttemptConfiguration(
                reasoning_effort=row.reasoning_effort,
                max_tokens=row.max_tokens,
            )
        )
        backend = f"harness:{row.harness}:{row.configuration}"
        identity = ModelCapabilityIdentity(
            model_revision=row.model,
            backend=backend,
            inference_configuration=configuration.configuration_id,
            suite_revision=suite_revision,
            grader_revision="deterministic-checker-v1",
            harness_revision=f"{row.harness}:{harness_revision}",
        )
        rows.append(
            ModelCapabilityVerdictRow(
                tenant_id=tenant_id,
                item_key=row.item_key,
                source=EnumCapabilitySource.LABELLED_ITEM,
                task_class=row.task_class,
                size_band=EnumSizeBand.S,
                model_key=row.model,
                backend_id=backend,
                configuration=configuration,
                identity=identity,
                passed=row.passed,
            )
        )
        assignments[(row.model, backend)] = ModelCapabilityServingAssignment(
            model_key=row.model,
            backend_id=backend,
            configuration_id=configuration.configuration_id,
            identity=identity,
        )
    digest = hashlib.sha256(
        json.dumps(sorted(r.model_dump_json() for r in rows)).encode()
    ).hexdigest()
    return ModelCapabilityMapRunRecorded(
        tenant_id=tenant_id,
        map_run_id=str(
            uuid5(
                NAMESPACE_URL,
                f"harness-capability:{suite_revision}:{harness_revision}:{digest}",
            )
        ),
        observed_at=observed_at,
        rows=tuple(rows),
        assignments=tuple(assignments[key] for key in sorted(assignments)),
    )


def normalize_observation(
    row: ModelHarnessObservation,
    *,
    diagnostic: str = "",
    error_class: str = "",
    transcript: str = "",
) -> ModelHarnessObservation:
    """Classify explicit startup and permission evidence without model judging.

    Raw receipts remain unchanged. A CLI that could not initialize never
    supplied a model trial; a tool permission rejection after startup remains
    a harness task failure. A passing checker always retains its pass.
    """
    if row.passed:
        return row
    kind: str | None = None
    available = row.available
    if any(
        f"failed to initialize in-process app-server client: {cause}" in diagnostic
        for cause in ("Read-only file system", "Operation not permitted")
    ):
        available, kind = False, "sandbox-initialization"
    elif error_class == "HarnessNotLoggedIn":
        available, kind = False, "not-logged-in"
    elif error_class in {
        "HarnessUnavailable",
        "HarnessExcluded",
        "HarnessPlacementRefused",
    }:
        available, kind = False, "harness-unavailable"
    elif (
        '"subtype":"permission_denied"' in transcript
        or '"subtype": "permission_denied"' in transcript
    ):
        kind = "permission-denied"
    return row.model_copy(
        update={"available": available, "failure_kind": kind or row.failure_kind}
    )
