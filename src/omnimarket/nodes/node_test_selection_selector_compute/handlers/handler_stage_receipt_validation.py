# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Validates the stage receipts a tree carries (operation ``validate_stage_receipt``).

A stage is entered one at a time, by a receipt that names its nights; the receipt's figures are not
trusted but recomputed from the nightly attribution records the source publishes, and every record
must hold the entry for the stage being entered at the receipt's selector and policy versions. Pure
over its request, its policy and its injected source. A source that cannot be read, or a night it
holds no record for, is a violation, never a pass.
"""

from __future__ import annotations

import datetime
import itertools
import math

from omnimarket.handlers.adapter_test_selection_policy_file import load_policy
from omnimarket.nodes.node_test_selection_selector_compute.adapters.source_attribution_records_in_memory import (
    SourceAttributionRecordsInMemory,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_published_attribution_record import (
    EnumRecordStatus,
    ModelPublishedAttributionRecord,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_stage_receipt import (
    EnumStageReceiptViolation,
    ModelStageReceipt,
    ModelStageReceiptValidationRequest,
    ModelStageReceiptValidationResult,
    ModelStageReceiptViolation,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_test_selection_selector import (
    ModelSelectorPolicy,
)
from omnimarket.nodes.node_test_selection_selector_compute.protocols.protocol_attribution_record_source import (
    ProtocolAttributionRecordSource,
)

_Code = EnumStageReceiptViolation


def _is_clean(record: ModelPublishedAttributionRecord) -> bool:
    return (
        record.status is EnumRecordStatus.COMPLETE
        and record.miss_count == 0
        and record.unattributable_count == 0
        and record.incomplete_count == 0
        and record.pull_request_record_count >= 1
    )


class HandlerStageReceiptValidation:
    """Pure over its request, its policy and its injected record source."""

    def __init__(
        self,
        source: ProtocolAttributionRecordSource | None = None,
        policy: ModelSelectorPolicy | None = None,
    ) -> None:
        # Without an injected source the gate holds a source that cannot be read, so it refuses.
        self._source = source or SourceAttributionRecordsInMemory()
        self._policy = policy or load_policy()

    def handle(
        self, request: ModelStageReceiptValidationRequest
    ) -> ModelStageReceiptValidationResult:
        violations: list[ModelStageReceiptViolation] = []
        by_stage: dict[int, ModelStageReceipt] = {}
        for receipt in request.receipts:
            if receipt.stage in by_stage:
                violations.append(
                    _violation(
                        _Code.DUPLICATE_RECEIPT,
                        receipt.stage,
                        "two receipts name the same stage",
                    )
                )
            by_stage[receipt.stage] = receipt
        for stage in range(request.current_stage + 1):
            if stage not in by_stage:
                violations.append(
                    _violation(
                        _Code.RECEIPT_MISSING,
                        stage,
                        f"selection_stage is {request.current_stage} and no receipt names stage {stage}",
                    )
                )
        base = request.base_stage
        if base is not None and request.current_stage - base > 1:
            violations.append(
                _violation(
                    _Code.STAGE_JUMP,
                    request.current_stage,
                    f"selection_stage went from {base} to {request.current_stage}; a stage is entered one at a time",
                )
            )
        first_checked = 1 if base is None else max(base + 1, 1)
        for stage in range(first_checked, request.current_stage + 1):
            if stage in by_stage:
                violations.extend(self._check_receipt(by_stage[stage]))
        return ModelStageReceiptValidationResult(
            valid=not violations, violations=tuple(violations)
        )

    def _check_receipt(
        self, receipt: ModelStageReceipt
    ) -> list[ModelStageReceiptViolation]:
        stage = receipt.stage
        found: list[ModelStageReceiptViolation] = []
        if receipt.entered_from_stage != stage - 1:
            found.append(
                _violation(
                    _Code.STAGE_JUMP,
                    stage,
                    f"the receipt enters stage {stage} from {receipt.entered_from_stage}, not from {stage - 1}",
                )
            )
        found.extend(self._check_targets(receipt))
        if (
            receipt.repository is None
            or receipt.selector_version is None
            or receipt.policy_version is None
        ):
            found.append(
                _violation(
                    _Code.RECEIPT_UNIDENTIFIED,
                    stage,
                    "the receipt names no repository, selector version or policy version",
                )
            )
            return found
        required = self._policy.stage_clean_nights
        if len(receipt.nights) < required:
            found.append(
                _violation(
                    _Code.NIGHTS_SHORT,
                    stage,
                    f"the receipt covers {len(receipt.nights)} nights; stage_clean_nights is {required}",
                )
            )
        slots = sorted(night.slot_date for night in receipt.nights)
        if any(
            later - earlier != datetime.timedelta(days=1)
            for earlier, later in itertools.pairwise(slots)
        ):
            found.append(
                _violation(
                    _Code.NIGHTS_NOT_CONSECUTIVE,
                    stage,
                    "the nights are not consecutive slot dates",
                )
            )
        run_ids = [night.nightly_run_id for night in receipt.nights]
        records = self._source.read_records(receipt.repository, run_ids)
        if records is None:
            found.append(
                _violation(
                    _Code.RECORDS_UNAVAILABLE,
                    stage,
                    "the published attribution records could not be read",
                )
            )
            return found
        found.extend(self._check_records(receipt, records))
        return found

    def _check_targets(
        self, receipt: ModelStageReceipt
    ) -> list[ModelStageReceiptViolation]:
        if receipt.stage == 0:
            return []
        measured = receipt.measured
        absent = [
            name
            for name in (
                "merge_narrowed_share",
                "test_seconds_per_merge",
                "ci_minutes_per_pr",
            )
            if measured is None or getattr(measured, name) is None
        ]
        if not absent:
            return []
        return [
            _violation(
                _Code.TARGETS_ABSENT,
                receipt.stage,
                "the receipt holds no measured value for " + ", ".join(absent),
            )
        ]

    def _check_records(
        self,
        receipt: ModelStageReceipt,
        records: tuple[ModelPublishedAttributionRecord, ...],
    ) -> list[ModelStageReceiptViolation]:
        stage = receipt.stage
        found: list[ModelStageReceiptViolation] = []
        by_run = {record.nightly_run_id: record for record in records}
        counted: list[ModelPublishedAttributionRecord] = []
        for night in receipt.nights:
            record = by_run.get(night.nightly_run_id)
            if record is None:
                found.append(
                    _violation(
                        _Code.RECORD_MISSING,
                        stage,
                        f"no published record for nightly run {night.nightly_run_id}",
                    )
                )
                continue
            counted.append(record)
            if record.nightly_slot_date != night.slot_date:
                found.append(
                    _violation(
                        _Code.RECORD_MISMATCH,
                        stage,
                        f"nightly run {night.nightly_run_id} was published for slot {record.nightly_slot_date}, not {night.slot_date}",
                    )
                )
            if record.stage_entry_counted != stage:
                found.append(
                    _violation(
                        _Code.RECORD_WRONG_STAGE_ENTRY,
                        stage,
                        f"nightly run {night.nightly_run_id} counts the entry of stage {record.stage_entry_counted}",
                    )
                )
            if (
                record.selector_version != receipt.selector_version
                or record.policy_version != receipt.policy_version
            ):
                found.append(
                    _violation(
                        _Code.VERSION_MISMATCH,
                        stage,
                        f"nightly run {night.nightly_run_id} ran selector {record.selector_version} and policy {record.policy_version}, not the receipt's",
                    )
                )
            if not _is_clean(record):
                found.append(
                    _violation(
                        _Code.NIGHT_NOT_CLEAN,
                        stage,
                        f"nightly run {night.nightly_run_id} is not a clean night",
                    )
                )
        found.extend(self._check_figures(receipt, counted))
        return found

    def _check_figures(
        self,
        receipt: ModelStageReceipt,
        counted: list[ModelPublishedAttributionRecord],
    ) -> list[ModelStageReceiptViolation]:
        stage = receipt.stage
        recomputed = {
            "clean_nights": sum(1 for record in counted if _is_clean(record)),
            "pr_record_count": sum(
                record.pull_request_record_count for record in counted
            ),
            "miss_total": sum(record.miss_count for record in counted),
            "unattributable_total": sum(
                record.unattributable_count for record in counted
            ),
            "incomplete_total": sum(record.incomplete_count for record in counted),
        }
        found: list[ModelStageReceiptViolation] = []
        wrong = [
            f"{name} {getattr(receipt, name)} (recomputed {value})"
            for name, value in recomputed.items()
            if getattr(receipt, name) != value
        ]
        share = (
            recomputed["clean_nights"] / len(receipt.nights) if receipt.nights else 0.0
        )
        if not math.isclose(receipt.clean_night_share, share, abs_tol=1e-6):
            wrong.append(
                f"clean_night_share {receipt.clean_night_share} (recomputed {round(share, 6)})"
            )
        if wrong:
            found.append(_violation(_Code.FIGURE_MISMATCH, stage, "; ".join(wrong)))
        if recomputed["pr_record_count"] < self._policy.stage_min_pr_records:
            found.append(
                _violation(
                    _Code.TOO_FEW_PR_RECORDS,
                    stage,
                    f"{recomputed['pr_record_count']} pull request records; stage_min_pr_records is {self._policy.stage_min_pr_records}",
                )
            )
        return found


def _violation(
    code: EnumStageReceiptViolation, stage: int, detail: str
) -> ModelStageReceiptViolation:
    return ModelStageReceiptViolation(code=code, stage=stage, detail=detail)
