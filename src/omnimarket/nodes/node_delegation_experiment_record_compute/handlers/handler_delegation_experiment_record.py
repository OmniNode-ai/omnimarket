# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Check the results records of the delegation experiment program (OMN-20422).

``handle`` takes the experiments the program lists, their results records and the receipt run
ids that exist, and returns the experiments whose record passes with their counted runs, and a typed
refusal for every defect of the others. It reads no file, calls no model and reads no clock.

The checks are the program's four acceptance criteria: AC1 a record per listed experiment with
hypothesis, metric, baseline, arms, per-arm values and a verdict; AC2 every counted run id has a
receipt; AC3 no changed path is the dev-lane overlay or the bifrost contract path; AC4 a supported
verdict names a follow-on or a declining ruling. A retired model on an arm is refused too.
"""

from __future__ import annotations

from collections import Counter

from omnimarket.models.delegation_experiment_record.enum_experiment_refusal import (
    EnumExperimentRefusal,
)
from omnimarket.models.delegation_experiment_record.enum_experiment_verdict import (
    EnumExperimentVerdict,
)
from omnimarket.models.delegation_experiment_record.model_experiment_issue import (
    ModelExperimentIssue,
)
from omnimarket.models.delegation_experiment_record.model_experiment_record import (
    ModelExperimentRecord,
)
from omnimarket.models.delegation_experiment_record.model_experiment_record_request import (
    ModelExperimentRecordRequest,
)
from omnimarket.models.delegation_experiment_record.model_experiment_record_result import (
    ModelExperimentRecordResult,
)
from omnimarket.models.delegation_experiment_record.model_experiment_run_count import (
    ModelExperimentRunCount,
)

_MIN_ARMS = 2


def _issue(
    refusal: EnumExperimentRefusal,
    record: ModelExperimentRecord,
    arm_id: str = "",
    detail: str = "",
) -> ModelExperimentIssue:
    return ModelExperimentIssue(
        refusal=refusal,
        experiment_id=record.experiment_id,
        arm_id=arm_id,
        detail=detail,
    )


def _record_issues(
    record: ModelExperimentRecord, request: ModelExperimentRecordRequest
) -> list[ModelExperimentIssue]:
    known = set(request.known_receipt_ids)
    issues: list[ModelExperimentIssue] = []
    if not record.hypothesis.strip():
        issues.append(_issue(EnumExperimentRefusal.MISSING_HYPOTHESIS, record))
    if not record.metric.strip():
        issues.append(_issue(EnumExperimentRefusal.MISSING_METRIC, record))
    if record.baseline is None:
        issues.append(_issue(EnumExperimentRefusal.MISSING_BASELINE, record))
    if record.verdict is None:
        issues.append(_issue(EnumExperimentRefusal.MISSING_VERDICT, record))
    if record.verdict is EnumExperimentVerdict.VOID and not record.void_cause.strip():
        issues.append(_issue(EnumExperimentRefusal.VOID_WITHOUT_CAUSE, record))
    if (
        record.verdict is EnumExperimentVerdict.SUPPORTED
        and not record.follow_on_refs
        and not record.declining_ruling_ref.strip()
    ):
        issues.append(_issue(EnumExperimentRefusal.SUPPORTED_WITHOUT_FOLLOW_ON, record))
    if len(record.arms) < _MIN_ARMS:
        issues.append(_issue(EnumExperimentRefusal.TOO_FEW_ARMS, record))
    arm_ids = Counter(arm.arm_id for arm in record.arms)
    issues.extend(
        _issue(EnumExperimentRefusal.DUPLICATE_ARM, record, arm_id=arm_id)
        for arm_id, count in sorted(arm_ids.items())
        if count > 1
    )
    run_ids = Counter(run_id for arm in record.arms for run_id in arm.run_ids)
    issues.extend(
        _issue(EnumExperimentRefusal.DUPLICATE_RUN, record, detail=run_id)
        for run_id, count in sorted(run_ids.items())
        if count > 1
    )
    for arm in record.arms:
        if not arm.run_ids:
            issues.append(_issue(EnumExperimentRefusal.EMPTY_ARM, record, arm.arm_id))
        if arm.per_arm_value is None:
            issues.append(
                _issue(EnumExperimentRefusal.MISSING_ARM_VALUE, record, arm.arm_id)
            )
        issues.extend(
            _issue(
                EnumExperimentRefusal.UNTRACEABLE_RUN,
                record,
                arm_id=arm.arm_id,
                detail=run_id,
            )
            for run_id in arm.run_ids
            if run_id not in known
        )
        model = arm.model.lower()
        issues.extend(
            _issue(
                EnumExperimentRefusal.RETIRED_MODEL,
                record,
                arm_id=arm.arm_id,
                detail=retired,
            )
            for retired in request.retired_models
            if retired.lower() in model
        )
    issues.extend(
        _issue(EnumExperimentRefusal.ROUTING_CHANGE, record, detail=path)
        for path in record.changed_paths
        if any(marker in path for marker in request.protected_path_markers)
    )
    return issues


class HandlerDelegationExperimentRecord:
    """Accept the experiment records that meet the program's acceptance criteria."""

    def handle(
        self, request: ModelExperimentRecordRequest
    ) -> ModelExperimentRecordResult:
        record_ids = Counter(record.experiment_id for record in request.records)
        issues: list[ModelExperimentIssue] = [
            ModelExperimentIssue(
                refusal=EnumExperimentRefusal.MISSING_RECORD, experiment_id=listed
            )
            for listed in request.listed_experiment_ids
            if listed not in record_ids
        ]
        accepted: list[str] = []
        counts: list[ModelExperimentRunCount] = []
        for record in request.records:
            if record_ids[record.experiment_id] > 1:
                issues.append(
                    _issue(EnumExperimentRefusal.DUPLICATE_EXPERIMENT, record)
                )
                continue
            record_issues = _record_issues(record, request)
            issues.extend(record_issues)
            if not record_issues:
                accepted.append(record.experiment_id)
                counts.append(
                    ModelExperimentRunCount(
                        experiment_id=record.experiment_id,
                        run_count=sum(len(arm.run_ids) for arm in record.arms),
                    )
                )
        return ModelExperimentRecordResult(
            accepted_experiment_ids=tuple(accepted),
            run_counts=tuple(counts),
            issues=tuple(issues),
        )
