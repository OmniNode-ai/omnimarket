# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Validates the stage receipts a tree carries (operation ``validate_stage_receipt``).

A stage is entered one at a time, by a receipt whose figures are not trusted but recomputed. Stage 1
is entered on one deterministic comparison over the recorded set: the gate replays the selector over
the recorded changes itself and refuses the stage when a test that failed in a change's recorded full
run is not in that change's selection; it reads no nightly record. A stage above 1 names its nights,
and its figures are recomputed from the nightly attribution records the source publishes, every
record holding the entry for the stage being entered at the receipt's selector and policy versions.
Pure over its request, its policy and its injected sources. A source that cannot be read, or a night
it holds no record for, is a violation, never a pass.
"""

from __future__ import annotations

import datetime
import itertools
import math

from omnimarket.nodes.node_test_selection_selector_compute.adapters.source_recorded_set_file import (
    SourceRecordedSetFile,
)
from omnimarket.nodes.node_test_selection_selector_compute.handlers.handler_selection_replay import (
    HandlerSelectionReplay,
)
from omnimarket.nodes.node_test_selection_selector_compute.handlers.handler_stage_comparison import (
    HandlerStageComparison,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_attribution_record_reading import (
    EnumRecordDefectKind,
    ModelAttributionRecordReading,
    ModelRecordDefect,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_published_attribution_record import (
    EnumRecordStatus,
    ModelPublishedAttributionRecord,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_recorded_set import (
    ModelRecordedSet,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_selection_replay import (
    ModelReplayFailed,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_stage_comparison import (
    ModelStageComparisonRequest,
    ModelStageComparisonResult,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_stage_receipt import (
    EnumStageReceiptViolation,
    ModelReceiptComparison,
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
from omnimarket.nodes.node_test_selection_selector_compute.protocols.protocol_recorded_set_source import (
    ProtocolRecordedSetSource,
)

_Code = EnumStageReceiptViolation
_FLOAT_TOLERANCE = 1e-6
#: The stage entered on the recorded-set comparison; a stage above it is entered on nightly records.
_COMPARISON_STAGE = 1

#: How a refused night is reported: a record that does not parse or does not match is a mismatch.
_DEFECT_CODE = {
    EnumRecordDefectKind.UNPARSEABLE: _Code.RECORD_MISMATCH,
    EnumRecordDefectKind.HEAD_MISMATCH: _Code.RECORD_MISMATCH,
    EnumRecordDefectKind.IDENTITY_MISMATCH: _Code.RECORD_MISMATCH,
    EnumRecordDefectKind.DUPLICATE: _Code.DUPLICATE_RECORD,
}


def _is_clean(record: ModelPublishedAttributionRecord) -> bool:
    return (
        record.status is EnumRecordStatus.COMPLETE
        and record.miss_count == 0
        and record.unattributable_count == 0
        and record.incomplete_count == 0
        and record.pull_request_record_count >= 1
    )


class HandlerStageReceiptValidation:
    """Pure over its request, its policy and its injected sources."""

    def __init__(
        self,
        source: ProtocolAttributionRecordSource | None = None,
        policy: ModelSelectorPolicy | None = None,
        recorded_source: ProtocolRecordedSetSource | None = None,
        selector_version: str | None = None,
        replay: HandlerSelectionReplay | None = None,
    ) -> None:
        # Imported here: the policy adapter and the gateway source reach this node's models, whose package
        # imports this module.
        from omnimarket.handlers.adapter_test_selection_policy_file import (
            load_policy,
            repo_root,
        )
        from omnimarket.handlers.adapter_test_selection_policy_file import (
            selector_version as read_selector_version,
        )
        from omnimarket.nodes.node_test_selection_selector_compute.adapters.source_attribution_records_gateway import (
            SourceAttributionRecordsGateway,
        )

        self._policy = policy or load_policy()
        # Without an injected source the gate reads the published records through the repository gateway
        # node; a gateway that gives no answer is an unreadable source, so the gate refuses.
        self._source = source or SourceAttributionRecordsGateway(policy=self._policy)
        # Stage 1 reads the recorded set from the checkout's own files, never a nightly record.
        self._recorded_source = recorded_source or SourceRecordedSetFile(repo_root())
        self._selector_version = selector_version or read_selector_version()
        self._replay = replay or HandlerSelectionReplay()

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
        if stage == _COMPARISON_STAGE:
            found.extend(self._check_comparison(receipt))
            return found
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
        reading = self._source.read_records(receipt.repository, receipt.nights)
        if reading is None:
            found.append(
                _violation(
                    _Code.RECORDS_UNAVAILABLE,
                    stage,
                    "the published attribution records could not be read",
                )
            )
            return found
        found.extend(self._check_records(receipt, reading))
        return found

    def _check_comparison(
        self, receipt: ModelStageReceipt
    ) -> list[ModelStageReceiptViolation]:
        stage = receipt.stage
        found: list[ModelStageReceiptViolation] = []
        if receipt.repository is None or receipt.selector_version is None:
            return [
                _violation(
                    _Code.RECEIPT_UNIDENTIFIED,
                    stage,
                    "the receipt names no repository or selector version",
                )
            ]
        if receipt.selector_version != self._selector_version:
            found.append(
                _violation(
                    _Code.VERSION_MISMATCH,
                    stage,
                    f"the receipt was made with selector {receipt.selector_version}, this tree holds {self._selector_version}",
                )
            )
        recorded = receipt.comparison
        if recorded is None:
            found.append(
                _violation(
                    _Code.COMPARISON_ABSENT,
                    stage,
                    "the receipt records no comparison over the recorded set",
                )
            )
            return found
        source = self._recorded_source.read_recorded_set(
            recorded.replay_input_path, recorded.observed_input_path
        )
        if source is None:
            found.append(
                _violation(
                    _Code.RECORDED_SET_UNAVAILABLE,
                    stage,
                    "the recorded set could not be read",
                )
            )
            return found
        digests = _digest_differences(recorded, source)
        if digests:
            found.append(_violation(_Code.INPUT_MISMATCH, stage, "; ".join(digests)))
            return found
        replayed = self._replay.handle(source.replay_input)
        if isinstance(replayed, ModelReplayFailed):
            found.append(
                _violation(
                    _Code.RECORDED_SET_UNAVAILABLE,
                    stage,
                    f"the replay failed: {replayed.reason}",
                )
            )
            return found
        result = HandlerStageComparison().handle(
            ModelStageComparisonRequest(
                replay=replayed,
                runs=source.runs,
                min_changes_compared=self._policy.stage_min_compared_changes,
            )
        )
        found.extend(_check_figures(receipt, recorded, result))
        if result.misses:
            found.append(
                _violation(
                    _Code.COMPARISON_MISS,
                    stage,
                    "a failing test is not in its change's selection: "
                    + "; ".join(
                        f"{miss.test} (change {miss.sha[:12]}, {miss.selection_kind})"
                        for miss in result.misses
                    ),
                )
            )
        if result.reasons:
            found.append(
                _violation(_Code.COMPARISON_FAILED, stage, "; ".join(result.reasons))
            )
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
        reading: ModelAttributionRecordReading,
    ) -> list[ModelStageReceiptViolation]:
        stage = receipt.stage
        found: list[ModelStageReceiptViolation] = []
        by_run: dict[int, list[ModelPublishedAttributionRecord]] = {}
        for published in reading.records:
            by_run.setdefault(published.nightly_run_id, []).append(published)
        refused: dict[int, ModelRecordDefect] = {}
        for defect in reading.defects:
            refused.setdefault(defect.nightly_run_id, defect)
            found.append(
                _violation(
                    _DEFECT_CODE[defect.kind],
                    stage,
                    f"nightly run {defect.nightly_run_id}, slot {defect.slot_date}: {defect.detail}",
                )
            )
        counted: list[ModelPublishedAttributionRecord] = []
        for night in receipt.nights:
            if night.nightly_run_id in refused:
                continue  # already refused with its own code; never counted
            published_here = by_run.get(night.nightly_run_id, [])
            if len(published_here) > 1:
                found.append(
                    _violation(
                        _Code.DUPLICATE_RECORD,
                        stage,
                        f"{len(published_here)} published records for nightly run {night.nightly_run_id}",
                    )
                )
                continue
            record = published_here[0] if published_here else None
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
            if record.tested_head_sha != night.tested_head_sha:
                found.append(
                    _violation(
                        _Code.RECORD_MISMATCH,
                        stage,
                        f"nightly run {night.nightly_run_id} was published for commit {record.tested_head_sha}, not the receipt's {night.tested_head_sha}",
                    )
                )
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


def _digest_differences(
    recorded: ModelReceiptComparison, source: ModelRecordedSet
) -> list[str]:
    differences: list[str] = []
    if recorded.replay_input_sha256 != source.replay_input_sha256:
        differences.append(
            f"replay input {recorded.replay_input_path} has digest {source.replay_input_sha256}, the receipt records {recorded.replay_input_sha256}"
        )
    if recorded.observed_input_sha256 != source.observed_input_sha256:
        differences.append(
            f"observed input {recorded.observed_input_path} has digest {source.observed_input_sha256}, the receipt records {recorded.observed_input_sha256}"
        )
    return differences


def _check_figures(
    receipt: ModelStageReceipt,
    recorded: ModelReceiptComparison,
    result: ModelStageComparisonResult,
) -> list[ModelStageReceiptViolation]:
    recomputed: dict[str, float | None] = {
        "changes_compared": result.changes_compared,
        "changes_narrowed": result.changes_narrowed,
        "runs_with_result": result.runs_with_result,
        "runs_unavailable": result.runs_unavailable,
        "failing_tests": result.failing_tests,
        "failing_tests_selected": result.failing_tests_selected,
        "misses": len(result.misses),
    }
    wrong = [
        f"{name} {getattr(recorded, name)} (recomputed {value})"
        for name, value in recomputed.items()
        if getattr(recorded, name) != value
    ]
    measured = receipt.measured
    if measured is not None:
        for name in (
            "merge_narrowed_share",
            "test_seconds_per_merge",
            "ci_minutes_per_pr",
        ):
            given = getattr(measured, name)
            value = getattr(result, name)
            if given is not None and (
                value is None
                or not math.isclose(given, value, abs_tol=_FLOAT_TOLERANCE)
            ):
                wrong.append(f"{name} {given} (recomputed {value})")
    if not wrong:
        return []
    return [_violation(_Code.FIGURE_MISMATCH, receipt.stage, "; ".join(wrong))]


def _violation(
    code: EnumStageReceiptViolation, stage: int, detail: str
) -> ModelStageReceiptViolation:
    return ModelStageReceiptViolation(code=code, stage=stage, detail=detail)
