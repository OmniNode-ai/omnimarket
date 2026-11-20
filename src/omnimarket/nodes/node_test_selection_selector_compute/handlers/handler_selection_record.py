# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Builds the record of one pull request run's test selection (operation ``build_selection_record``).

The record holds the executed selection (what pytest ran) and the selection of every stage (what the
rules at or below that stage give, computed as if the flag were on). The stage gate is here: the
executed selection is the entry of the policy's ``selection_stage`` when the flag is on and the full
matrix when it is not, so the pytest matrix is built from one entry whatever the later stages hold.
Pure over its request, its policy and its injected selector; the workflow's file reads and writes are
in ``adapters/adapter_selection_record_cli.py``.
"""

from __future__ import annotations

from omnimarket.handlers.adapter_test_selection_policy_file import (
    load_policy,
)
from omnimarket.nodes.node_test_selection_selector_compute.handlers.handler_test_selection_selector import (
    HandlerTestSelectionSelector,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_test_selection_record import (
    EnumFullSuiteReason,
    EnumGraphSource,
    ModelSelectionRecordRequest,
    ModelStageSelection,
    ModelTestSelection,
    ModelTestSelectionRecord,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_test_selection_selector import (
    ModelSelectorPolicy,
    ModelSelectorRequest,
)


def build_artifact_name(pr_number: int | None, run_id: int, run_attempt: int) -> str:
    """The artifact a run's record is uploaded as; the workflow's ``name:`` renders the same text.

    A run id belongs to one repository and one head sha, so the name is unique per
    ``(repo, pr, head_sha, run_id, run_attempt)``: a rerun is a new attempt and a new name.
    """
    return f"test-selection-{pr_number or 0}-{run_id}-{run_attempt}"


class HandlerSelectionRecord:
    """Pure over its request, its policy and its injected selector."""

    def __init__(
        self,
        selector: HandlerTestSelectionSelector | None = None,
        policy: ModelSelectorPolicy | None = None,
    ) -> None:
        self._policy = policy or load_policy()
        self._selector = selector or HandlerTestSelectionSelector(None, self._policy)

    def handle(self, request: ModelSelectionRecordRequest) -> ModelTestSelectionRecord:
        by_stage = {
            stage: self._entry(selection, request)
            for stage, selection in request.stage_selections.items()
        }
        stage = self._policy.selection_stage
        if not request.flag_enabled:
            executed = self._full(EnumFullSuiteReason.FEATURE_FLAG_OFF, request)
        elif stage in by_stage:
            executed = by_stage[stage]
        else:
            executed = self._full(EnumFullSuiteReason.SELECTION_UNAVAILABLE, request)
        return ModelTestSelectionRecord(
            repo=request.repo,
            pr_number=request.pr_number,
            head_sha=request.head_sha,
            base_sha=request.base_sha,
            checkout_sha=request.checkout_sha,
            run_id=request.run_id,
            run_attempt=request.run_attempt,
            changed_files=request.changed_files,
            policy_version=request.policy_version,
            selection_stage=stage,
            executed_selection=executed,
            selection_by_stage=by_stage,
            graph_source=EnumGraphSource.NONE,
        )

    def _full(
        self, reason: EnumFullSuiteReason, request: ModelSelectionRecordRequest
    ) -> ModelStageSelection:
        return self._entry(
            ModelTestSelection.full_suite(reason, self._policy.full_split_count),
            request,
        )

    def _entry(
        self, selection: ModelTestSelection, request: ModelSelectionRecordRequest
    ) -> ModelStageSelection:
        decision = self._selector.handle(
            ModelSelectorRequest(
                selected_paths=tuple(selection.selected_paths),
                repo_root=request.repo_root,
            )
        )
        return ModelStageSelection.from_selection(
            selection,
            selected_seconds=decision.estimated_seconds,
            selector_version=request.selector_version,
            policy_version=request.policy_version,
        )
