# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""selection_verdict: the written verdict over the comparison rows, in two parts.

A pure function of the rows, the window and the policy's minimum; it reads no clock, file or network.
"""

from __future__ import annotations

from collections.abc import Sequence

from omnimarket.models.model_code_graph_selection_compare import (
    EnumCompareOutcome,
    EnumGraphAnswerSource,
    ModelSelectionCompareRow,
)
from omnimarket.models.model_code_graph_selection_verdict import (
    EnumPartOneVerdict,
    EnumPartTwoVerdict,
    ModelSelectionVerdict,
    ModelSelectionVerdictRequest,
)


class SelectionVerdict:
    """Judge whether the graph found tests the parse missed, and whether one of them later failed."""

    def handle(self, request: ModelSelectionVerdictRequest) -> ModelSelectionVerdict:
        rows = request.rows
        valid = [row for row in rows if row.valid_for_verdict]
        days = (request.now - request.window_start).days

        graph_only = {
            (row.repo, row.pr_number, path)
            for row in valid
            for path in row.graph_only_paths
            if row.pr_number is not None
        }
        part_one = self._part_one(
            request, days, len(valid), sum(r.graph_only for r in valid)
        )
        failed = tuple(
            sorted(
                f"{failure.repo}#{failure.pr_number}:{failure.test_path}"
                for failure in request.nightly_failures
                if (failure.repo, failure.pr_number, failure.test_path) in graph_only
            )
        )
        part_two = self._part_two(request, bool(graph_only), failed)
        return ModelSelectionVerdict(
            part_one=part_one,
            part_two=part_two,
            days_elapsed=days,
            window_days=request.window_days,
            nights_observed=request.nightly_nights_observed,
            nights_required=request.nightly_nights_required,
            graph_min_valid_comparisons=request.graph_min_valid_comparisons,
            valid_comparisons=len(valid),
            graph_only_total=sum(r.graph_only for r in valid),
            parse_only_total=sum(r.parse_only for r in valid),
            both_total=sum(r.both for r in valid),
            excluded_snapshot_mismatch=_count(
                rows, EnumCompareOutcome.SNAPSHOT_MISMATCH
            ),
            excluded_file_store=sum(
                1
                for r in rows
                if r.outcome is EnumCompareOutcome.COMPARED
                and r.graph_source is EnumGraphAnswerSource.FILE_STORE
            ),
            excluded_historical=_count(rows, EnumCompareOutcome.HISTORICAL_UNVERSIONED),
            excluded_lookup_failed=_count(rows, EnumCompareOutcome.LOOKUP_FAILED),
            graph_only_failures=failed,
        )

    @staticmethod
    def _part_one(
        request: ModelSelectionVerdictRequest,
        days: int,
        valid: int,
        graph_only_total: int,
    ) -> EnumPartOneVerdict:
        if days < request.window_days:
            return EnumPartOneVerdict.PENDING
        if valid < request.graph_min_valid_comparisons:
            return EnumPartOneVerdict.INSUFFICIENT
        return (
            EnumPartOneVerdict.GRAPH_FOUND_TESTS_PARSE_MISSED
            if graph_only_total
            else EnumPartOneVerdict.GRAPH_FOUND_NOTHING_EXTRA
        )

    @staticmethod
    def _part_two(
        request: ModelSelectionVerdictRequest,
        has_graph_only: bool,
        failed: tuple[str, ...],
    ) -> EnumPartTwoVerdict:
        if failed:
            return EnumPartTwoVerdict.GRAPH_ONLY_TEST_FAILED
        observed = request.nightly_nights_observed
        if observed is None or observed < request.nightly_nights_required:
            return EnumPartTwoVerdict.PENDING
        return (
            EnumPartTwoVerdict.NO_GRAPH_ONLY_TEST_FAILED
            if has_graph_only
            else EnumPartTwoVerdict.NOT_APPLICABLE
        )


def _count(
    rows: Sequence[ModelSelectionCompareRow], outcome: EnumCompareOutcome
) -> int:
    return sum(1 for row in rows if row.outcome is outcome)
