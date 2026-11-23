# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Validates the stage receipts a tree carries (operation ``validate_stage_receipt``).

A stage is entered one at a time, by a receipt that records one comparison over the recorded set: the selection
the selector gives for each recorded change against the failures the full run of that change recorded. The
receipt's figures are not trusted but recomputed here from the recorded set the source reads (the same replay
and comparison handlers every run uses), and the comparison must pass. Pure over its request, its policy and its
injected source. A set that cannot be read is a violation, never a pass.
"""

from __future__ import annotations

import math

from omnimarket.nodes.node_test_selection_selector_compute.adapters.source_recorded_set_in_memory import (
    SourceRecordedSetInMemory,
)
from omnimarket.nodes.node_test_selection_selector_compute.handlers.handler_selection_replay import (
    HandlerSelectionReplay,
)
from omnimarket.nodes.node_test_selection_selector_compute.handlers.handler_stage_comparison import (
    HandlerStageComparison,
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
from omnimarket.nodes.node_test_selection_selector_compute.protocols.protocol_recorded_set_source import (
    ProtocolRecordedSetSource,
)

_Code = EnumStageReceiptViolation
_FLOAT_TOLERANCE = 1e-6


class HandlerStageReceiptValidation:
    """Pure over its request, its policy and its injected record source."""

    def __init__(
        self,
        source: ProtocolRecordedSetSource | None = None,
        policy: ModelSelectorPolicy | None = None,
        selector_version: str | None = None,
        replay: HandlerSelectionReplay | None = None,
    ) -> None:
        # Imported here: the policy adapter reaches this node's models, whose package imports this module.
        from omnimarket.handlers.adapter_test_selection_policy_file import (
            load_policy,
        )
        from omnimarket.handlers.adapter_test_selection_policy_file import (
            selector_version as read_selector_version,
        )

        # Without an injected source the gate holds a source that cannot be read, so it refuses.
        self._source = source or SourceRecordedSetInMemory()
        self._policy = policy or load_policy()
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
        if receipt.repository is None or receipt.selector_version is None:
            found.append(
                _violation(
                    _Code.RECEIPT_UNIDENTIFIED,
                    stage,
                    "the receipt names no repository or selector version",
                )
            )
            return found
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
        source = self._source.read_recorded_set(
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
        if not result.passed:
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
