# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Census of a code eval suite: per-band and per-repository counts, exclusions, and candidate rows."""

from __future__ import annotations

import hashlib
from collections import Counter

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)
from omnimarket.models.code_eval_suite.enum_suite_census_failure import (
    EnumSuiteCensusFailure,
)
from omnimarket.models.code_eval_suite.model_suite_census_request import (
    ModelCodeEvalSuiteCensusRequest,
)
from omnimarket.models.code_eval_suite.model_suite_census_result import (
    ModelCodeEvalBandCount,
    ModelCodeEvalBandRepositoryCount,
    ModelCodeEvalControlCounts,
    ModelCodeEvalReasonCount,
    ModelCodeEvalRepositoryCount,
    ModelCodeEvalSuiteCandidate,
    ModelCodeEvalSuiteCensus,
)
from omnimarket.models.code_eval_suite.model_suite_overlay import (
    ModelCodeEvalSuiteCensusOverlay,
)
from omnimarket.models.code_eval_suite.model_suite_task import (
    ModelCodeEvalSuiteTask,
)
from omnimarket.models.delegation.model_size_band import EnumSizeBand
from omnimarket.models.enum_code_score import (
    EnumCodeScoreSource,
    EnumCodeScoreVerdict,
)
from omnimarket.ranges.compare import required_comparison_size
from omnimarket.ranges.power import required_sample_size

_OVERLAY_ID = "node_code_eval_suite_census_compute"


class HandlerCodeEvalSuiteCensus:
    """Answers a census request from its own overlay line, with no clock and no model call."""

    def __init__(self, overlay: ModelCodeEvalSuiteCensusOverlay | None = None) -> None:
        self._overlay = overlay

    def handle(
        self, request: ModelCodeEvalSuiteCensusRequest
    ) -> ModelCodeEvalSuiteCensus:
        suite_id = request.suite_id
        if not request.tasks:
            return self._failure(
                suite_id,
                EnumSuiteCensusFailure.NO_TASKS,
                "the request carries no tasks",
            )
        duplicate = self._first_duplicate(request.tasks)
        if duplicate is not None:
            return self._failure(
                suite_id, EnumSuiteCensusFailure.DUPLICATE_TASK, duplicate
            )
        try:
            overlay = self._overlay or load_structured_overlay(
                _OVERLAY_ID, ModelCodeEvalSuiteCensusOverlay
            )
        except NodeOverlayError as error:
            return self._failure(
                suite_id, EnumSuiteCensusFailure.OVERLAY_UNAVAILABLE, str(error)
            )
        try:
            required_cell_n = required_sample_size(
                floor=overlay.line.floor,
                margin=overlay.line.margin,
                confidence=overlay.line.confidence,
                power=overlay.line.power,
            )
            required_paired_n = required_comparison_size(
                margin=overlay.line.margin,
                confidence=overlay.line.confidence,
                power=overlay.line.power,
            )
        except ValueError as error:
            return self._failure(
                suite_id, EnumSuiteCensusFailure.OVERLAY_UNAVAILABLE, str(error)
            )
        return self._census(request, required_cell_n, required_paired_n)

    def _first_duplicate(self, tasks: tuple[ModelCodeEvalSuiteTask, ...]) -> str | None:
        seen_commits: set[str] = set()
        seen_repo_contract: set[tuple[str, str]] = set()
        for task in tasks:
            if task.merge_commit in seen_commits:
                return f"merge_commit {task.merge_commit} is repeated"
            seen_commits.add(task.merge_commit)
            key = (task.repository, task.contract_id)
            if key in seen_repo_contract:
                return f"repository {task.repository} contract {task.contract_id} is repeated"
            seen_repo_contract.add(key)
        return None

    def _failure(
        self, suite_id: str, failure: EnumSuiteCensusFailure, detail: str
    ) -> ModelCodeEvalSuiteCensus:
        return ModelCodeEvalSuiteCensus(
            suite_id=suite_id,
            suite_digest=hashlib.sha256(suite_id.encode("utf-8")).hexdigest(),
            candidates_total=0,
            failure=failure,
            detail=detail,
        )

    def _census(
        self,
        request: ModelCodeEvalSuiteCensusRequest,
        required_cell_n: int,
        required_paired_n: int,
    ) -> ModelCodeEvalSuiteCensus:
        tasks = request.tasks
        admitted = tuple(
            sorted(
                (t for t in tasks if t.control is EnumCodeScoreVerdict.PASS),
                key=lambda t: (t.repository, t.merge_commit),
            )
        )
        control = self._control_counts(tasks)
        excluded = self._excluded(request)
        by_band = self._by_band(admitted, required_cell_n, required_paired_n)
        by_repository = self._by_repository(tasks, admitted)
        by_band_repository = self._by_band_repository(admitted)
        candidates = tuple(
            ModelCodeEvalSuiteCandidate(
                repository=t.repository,
                merge_commit=t.merge_commit,
                contract_id=t.contract_id,
            )
            for t in admitted
        )
        candidates_total = len(tasks) + sum(
            e.count for e in request.pre_control_exclusions
        )
        return ModelCodeEvalSuiteCensus(
            suite_id=request.suite_id,
            suite_digest=self._digest(tasks),
            candidates_total=candidates_total,
            control=control,
            by_band=by_band,
            by_repository=by_repository,
            by_band_repository=by_band_repository,
            excluded=excluded,
            candidates=candidates,
        )

    def _control_counts(
        self, tasks: tuple[ModelCodeEvalSuiteTask, ...]
    ) -> ModelCodeEvalControlCounts:
        holds = 0
        does_not_hold = 0
        could_not_run = 0
        for task in tasks:
            if task.control is EnumCodeScoreVerdict.PASS:
                holds += 1
            elif task.control is EnumCodeScoreVerdict.UNSCORABLE:
                if task.control_source is EnumCodeScoreSource.FALSIFIER_NOT_HELD:
                    does_not_hold += 1
                else:
                    could_not_run += 1
            else:
                does_not_hold += 1
        return ModelCodeEvalControlCounts(
            holds=holds, does_not_hold=does_not_hold, could_not_run=could_not_run
        )

    def _excluded(
        self, request: ModelCodeEvalSuiteCensusRequest
    ) -> tuple[ModelCodeEvalReasonCount, ...]:
        reasons: Counter[str] = Counter()
        for task in request.tasks:
            if task.control is EnumCodeScoreVerdict.PASS:
                continue
            if (
                task.control is EnumCodeScoreVerdict.UNSCORABLE
                and task.control_source is not None
            ):
                reasons[f"control:unscorable:{task.control_source.value}"] += 1
            else:
                reasons[f"control:{task.control.value}"] += 1
        for exclusion in request.pre_control_exclusions:
            reasons[f"pre_control:{exclusion.reason}"] += exclusion.count
        return tuple(
            ModelCodeEvalReasonCount(reason=reason, count=count)
            for reason, count in sorted(reasons.items())
        )

    def _by_band(
        self,
        admitted: tuple[ModelCodeEvalSuiteTask, ...],
        required_cell_n: int,
        required_paired_n: int,
    ) -> tuple[ModelCodeEvalBandCount, ...]:
        counts = Counter(t.band for t in admitted)
        return tuple(
            ModelCodeEvalBandCount(
                band=band,
                admitted=counts[band],
                required_cell_n=required_cell_n,
                cell_shortfall=max(0, required_cell_n - counts[band]),
                required_paired_n=required_paired_n,
                paired_shortfall=max(0, required_paired_n - counts[band]),
            )
            for band in EnumSizeBand
        )

    def _by_repository(
        self,
        tasks: tuple[ModelCodeEvalSuiteTask, ...],
        admitted: tuple[ModelCodeEvalSuiteTask, ...],
    ) -> tuple[ModelCodeEvalRepositoryCount, ...]:
        repositories = sorted({t.repository for t in tasks})
        admitted_counts = Counter(t.repository for t in admitted)
        return tuple(
            ModelCodeEvalRepositoryCount(
                repository=repository, admitted=admitted_counts[repository]
            )
            for repository in repositories
        )

    def _by_band_repository(
        self, admitted: tuple[ModelCodeEvalSuiteTask, ...]
    ) -> tuple[ModelCodeEvalBandRepositoryCount, ...]:
        counts: Counter[tuple[EnumSizeBand, str]] = Counter(
            (t.band, t.repository) for t in admitted
        )
        return tuple(
            ModelCodeEvalBandRepositoryCount(
                band=band, repository=repository, admitted=count
            )
            for (band, repository), count in sorted(
                counts.items(),
                key=lambda item: (list(EnumSizeBand).index(item[0][0]), item[0][1]),
            )
        )

    def _digest(self, tasks: tuple[ModelCodeEvalSuiteTask, ...]) -> str:
        rows = sorted(
            f"{t.repository}|{t.merge_commit}|{t.contract_id}|{t.control.value}"
            for t in tasks
        )
        return hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest()
