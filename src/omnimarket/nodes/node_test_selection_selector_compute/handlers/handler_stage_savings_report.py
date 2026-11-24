# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
# unimported-handler-allow: HandlerStageSavingsReport is resolved at boot from handler_routing in contract.yaml, by module path, not by a Python import
"""Reports what test selection saved against the full suite, per merged pull request (operation ``report_stage_savings``).

Every read goes through the repository gateway node: the merged pull requests (``pr_heads``), the CI runs of each
merged head (``workflow_runs``) and the selection record artifact of the latest run (``artifacts``). Nothing else
is read and no call of its own is made to GitHub. The full-suite seconds are the selector's own estimate for
``tests/`` over the durations file of the checkout, the same estimate a record's full-suite entry holds.

A pull request is counted only when its record is read and agrees with its run: an absent, expired, unparseable
or mismatched record, a record without seconds, a record whose selected seconds exceed the full-suite seconds and
a gateway that gives no answer for the pull request each make an unreadable row with its reason, and that row is
in no total. Nothing is estimated for a pull request that has no record.
"""

from __future__ import annotations

import base64
import binascii
import datetime
import io
import re
import zipfile
from collections import Counter

from pydantic import ValidationError

from omnimarket.handlers.adapter_test_selection_policy_file import load_policy
from omnimarket.handlers.scm.handler_gateway_read_client import HandlerGatewayReadClient
from omnimarket.models.model_gateway_node_skew import GatewayReadError
from omnimarket.models.model_github_gateway_io import (
    MAX_LIST_LIMIT,
    EnumGithubGatewayOperation,
    ModelArtifactsResult,
    ModelArtifactSummary,
    ModelGithubGatewayRequest,
    ModelPrHead,
    ModelPrHeadsResult,
    ModelWorkflowRunsResult,
    ModelWorkflowRunSummary,
)
from omnimarket.nodes.node_test_selection_selector_compute.handlers.handler_test_selection_selector import (
    HandlerTestSelectionSelector,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_stage_savings_report import (
    EnumSavingsRowStatus,
    EnumSavingsUnreadableReason,
    ModelPrSavingsRow,
    ModelStageSavingsReport,
    ModelStageSavingsReportRequest,
    ModelStageSavingsTotals,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_test_selection_record import (
    EnumSelectionKind,
    ModelTestSelectionRecord,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_test_selection_selector import (
    ModelSelectorPolicy,
    ModelSelectorRequest,
)
from omnimarket.nodes.node_test_selection_selector_compute.protocols.protocol_gateway_read import (
    ProtocolGatewayRead,
)

_Op = EnumGithubGatewayOperation
_Reason = EnumSavingsUnreadableReason
_WHOLE_TREE = "tests/"
#: ``test-selection-<pr>-<run id>-<attempt>``: the name build_artifact_name gives a record's artifact.
_ARTIFACT_NAME = re.compile(r"^test-selection-(\d+)-(\d+)-(\d+)$")
_PLACES = 6


class _UnreadableError(Exception):
    """A pull request whose record cannot be counted: the row's reason and detail."""

    def __init__(
        self,
        reason: EnumSavingsUnreadableReason,
        detail: str,
        run_id: int | None = None,
        attempt: int | None = None,
    ) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail
        self.run_id = run_id
        self.attempt = attempt


class HandlerStageSavingsReport:
    """Reads through its injected gateway; the settings come from the policy."""

    def __init__(
        self,
        gateway: ProtocolGatewayRead | None = None,
        policy: ModelSelectorPolicy | None = None,
        selector: HandlerTestSelectionSelector | None = None,
    ) -> None:
        self._gateway = gateway if gateway is not None else HandlerGatewayReadClient()
        self._policy = policy or load_policy()
        # A policy without the report keys cannot report: refuse at construction, not mid-read.
        self._policy.savings_report_settings()
        self._selector = selector or HandlerTestSelectionSelector(None, self._policy)

    def handle(
        self, request: ModelStageSavingsReportRequest
    ) -> ModelStageSavingsReport:
        first_default, workflow, limit, cap = self._policy.savings_report_settings()
        first = request.first_pr_number or first_default
        full = self._full_suite_seconds(request.repo_root)
        anchor, merged = self._merged_since(request.repository, first, limit)
        assert anchor.merged_at is not None
        rows = tuple(
            self._row(request.repository, pr, workflow, cap, full) for pr in merged
        )
        return ModelStageSavingsReport(
            repository=request.repository,
            first_pr_number=first,
            first_pr_merged_at=anchor.merged_at,
            full_suite_seconds=full,
            rows=rows,
            totals=_totals(rows),
        )

    def _full_suite_seconds(self, repo_root: str) -> float:
        decision = self._selector.handle(
            ModelSelectorRequest(selected_paths=(_WHOLE_TREE,), repo_root=repo_root)
        )
        if decision.estimated_seconds is None:
            raise ValueError(
                f"the full-suite seconds cannot be read from the durations of {repo_root}"
            )
        return decision.estimated_seconds

    def _merged_since(
        self, repository: str, first: int, limit: int
    ) -> tuple[ModelPrHead, list[ModelPrHead]]:
        result = self._gateway.read(
            ModelGithubGatewayRequest(
                operation=_Op.PR_HEADS, repo=repository, limit=limit
            )
        )
        if not isinstance(result, ModelPrHeadsResult):
            raise GatewayReadError(
                f"gateway answered {result.operation!r} for pr_heads"
            )
        anchor = next(
            (
                pr
                for pr in result.prs
                if pr.number == first
                and pr.state == "MERGED"
                and pr.merged_at is not None
            ),
            None,
        )
        if anchor is None or anchor.merged_at is None:
            raise ValueError(
                f"pull request {first} is not among the {len(result.prs)} newest pull requests the gateway "
                f"listed as merged (limit {limit}, truncated {result.truncated}); the report is not truncated to fit"
            )
        since = anchor.merged_at
        merged = [
            pr
            for pr in result.prs
            if pr.state == "MERGED"
            and pr.merged_at is not None
            and pr.merged_at >= since
            and (
                pr.base_ref is None
                or anchor.base_ref is None
                or pr.base_ref == anchor.base_ref
            )
        ]
        return anchor, sorted(merged, key=lambda pr: pr.number)

    def _row(
        self, repository: str, pr: ModelPrHead, workflow: str, cap: int, full: float
    ) -> ModelPrSavingsRow:
        try:
            return self._read_row(repository, pr, workflow, cap, full)
        except _UnreadableError as refused:
            return ModelPrSavingsRow(
                pr_number=pr.number,
                head_sha=pr.head_oid,
                status=EnumSavingsRowStatus.UNREADABLE,
                unreadable_reason=refused.reason,
                unreadable_detail=refused.detail,
                run_id=refused.run_id,
                run_attempt=refused.attempt,
            )
        except GatewayReadError as exc:
            return ModelPrSavingsRow(
                pr_number=pr.number,
                head_sha=pr.head_oid,
                status=EnumSavingsRowStatus.UNREADABLE,
                unreadable_reason=_Reason.GATEWAY_UNREADABLE,
                unreadable_detail=str(exc),
            )

    def _read_row(
        self, repository: str, pr: ModelPrHead, workflow: str, cap: int, full: float
    ) -> ModelPrSavingsRow:
        run, summary, attempt = self._latest_record_artifact(repository, pr, workflow)
        if run.status != "completed":
            raise _UnreadableError(
                _Reason.RUN_UNFINISHED, f"the run is {run.status}", run.id, attempt
            )
        if summary.expired:
            raise _UnreadableError(
                _Reason.RECORD_EXPIRED, f"{summary.name} has expired", run.id, attempt
            )
        record = self._fetch_record(repository, run.id, summary, cap, attempt)
        _check_identity(record, repository, pr, run.id, attempt)
        executed = record.executed_selection
        if executed.selected_seconds is None:
            raise _UnreadableError(
                _Reason.SELECTED_SECONDS_UNREAD,
                "the record's executed selection holds no seconds",
                run.id,
                attempt,
            )
        recorded = executed.selected_seconds
        ran_everything = executed.kind is EnumSelectionKind.FULL_SUITE
        # A narrowed run may not cost more than the suite. A full-suite run's own estimate is the suite under the
        # durations of its day, so it may differ from today's by the policy's record tolerance, and no more.
        ceiling = (
            full * (1 + self._policy.record_tolerance_ratio) if ran_everything else full
        )
        if recorded > ceiling:
            raise _UnreadableError(
                _Reason.SELECTED_EXCEEDS_FULL,
                f"the record's selected seconds {recorded} exceed the full-suite seconds {full}"
                + (
                    f" by more than the tolerance {self._policy.record_tolerance_ratio}"
                    if ran_everything
                    else ""
                ),
                run.id,
                attempt,
            )
        selected = full if ran_everything else recorded
        return ModelPrSavingsRow(
            pr_number=pr.number,
            head_sha=pr.head_oid,
            status=EnumSavingsRowStatus.READ,
            run_id=run.id,
            run_attempt=attempt,
            selection_stage=record.selection_stage,
            executed_kind=executed.kind,
            selected_seconds=selected,
            recorded_selected_seconds=recorded,
            full_suite_seconds=full,
            seconds_saved=round(full - selected, _PLACES),
            ci_minutes=round(_run_minutes(run), _PLACES),
        )

    def _latest_record_artifact(
        self, repository: str, pr: ModelPrHead, workflow: str
    ) -> tuple[ModelWorkflowRunSummary, ModelArtifactSummary, int]:
        runs_result = self._gateway.read(
            ModelGithubGatewayRequest(
                operation=_Op.WORKFLOW_RUNS,
                repo=repository,
                head_sha=pr.head_oid,
                workflow=workflow,
                event="pull_request",
                limit=MAX_LIST_LIMIT,
            )
        )
        if not isinstance(runs_result, ModelWorkflowRunsResult):
            raise GatewayReadError(
                f"gateway answered {runs_result.operation!r} for workflow_runs"
            )
        runs = [run for run in runs_result.runs if run.head_sha == pr.head_oid]
        if not runs:
            raise _UnreadableError(
                _Reason.NO_RUN, f"no {workflow} run on the merged head {pr.head_oid}"
            )
        found: list[tuple[int, int, ModelWorkflowRunSummary, ModelArtifactSummary]] = []
        for run in runs:
            listing = self._gateway.read(
                ModelGithubGatewayRequest(
                    operation=_Op.ARTIFACTS,
                    repo=repository,
                    run_id=run.id,
                    limit=MAX_LIST_LIMIT,
                )
            )
            if not isinstance(listing, ModelArtifactsResult):
                raise GatewayReadError(
                    f"gateway answered {listing.operation!r} for artifacts"
                )
            for artifact in listing.artifacts:
                named = _ARTIFACT_NAME.match(artifact.name)
                if named and int(named[1]) == pr.number and int(named[2]) == run.id:
                    found.append((run.id, int(named[3]), run, artifact))
        if not found:
            raise _UnreadableError(
                _Reason.NO_RECORD,
                f"no selection record artifact for pull request {pr.number} in {len(runs)} run(s) of the merged head",
            )
        _, attempt, run, artifact = max(found, key=lambda item: (item[0], item[1]))
        return run, artifact, attempt

    def _fetch_record(
        self,
        repository: str,
        run_id: int,
        summary: ModelArtifactSummary,
        cap: int,
        attempt: int,
    ) -> ModelTestSelectionRecord:
        result = self._gateway.read(
            ModelGithubGatewayRequest(
                operation=_Op.ARTIFACTS,
                repo=repository,
                run_id=run_id,
                artifact_id=summary.id,
                max_bytes=cap,
                limit=MAX_LIST_LIMIT,
            )
        )
        if not isinstance(result, ModelArtifactsResult):
            raise GatewayReadError(
                f"gateway answered {result.operation!r} for artifacts"
            )
        if result.fetched is None:
            refusal = (
                result.fetch_refused.value if result.fetch_refused else "no archive"
            )
            raise _UnreadableError(
                _Reason.RECORD_REFUSED,
                f"the gateway did not hand over {summary.name}: {refusal}",
                run_id,
                attempt,
            )
        return _parse_archive(
            result.fetched.content_base64, summary.name, cap, run_id, attempt
        )


def _parse_archive(
    content_base64: str, name: str, cap: int, run_id: int, attempt: int
) -> ModelTestSelectionRecord:
    def unparseable(detail: str) -> _UnreadableError:
        return _UnreadableError(
            _Reason.RECORD_UNPARSEABLE, f"{name}: {detail}", run_id, attempt
        )

    try:
        archive = zipfile.ZipFile(io.BytesIO(base64.b64decode(content_base64)))
    except (binascii.Error, zipfile.BadZipFile) as exc:
        raise unparseable(f"not a zip archive ({type(exc).__name__})") from exc
    with archive:
        members = [
            info for info in archive.infolist() if info.filename.endswith(".json")
        ]
        if len(members) != 1:
            raise unparseable(f"{len(members)} JSON files in the archive, one expected")
        if members[0].file_size > cap:
            raise unparseable("the JSON file is larger than the size cap")
        body = archive.read(members[0])
    try:
        return ModelTestSelectionRecord.model_validate_json(body)
    except ValidationError as exc:
        raise unparseable(
            f"not a selection record ({exc.error_count()} errors)"
        ) from exc


def _check_identity(
    record: ModelTestSelectionRecord,
    repository: str,
    pr: ModelPrHead,
    run_id: int,
    attempt: int,
) -> None:
    if (
        record.repo != repository
        or record.pr_number != pr.number
        or record.head_sha != pr.head_oid
        or record.run_id != run_id
        or record.run_attempt != attempt
    ):
        raise _UnreadableError(
            _Reason.RECORD_IDENTITY_MISMATCH,
            "the record names another repository, pull request, head commit, run or attempt than its artifact",
            run_id,
            attempt,
        )


def _run_minutes(run: ModelWorkflowRunSummary) -> float:
    """Minutes between the run's creation and its last update: the figure the stage comparison calls wall minutes."""
    start = datetime.datetime.fromisoformat(run.created_at)
    end = datetime.datetime.fromisoformat(run.updated_at)
    return (end - start).total_seconds() / 60


def _totals(rows: tuple[ModelPrSavingsRow, ...]) -> ModelStageSavingsTotals:
    read = [row for row in rows if row.status is EnumSavingsRowStatus.READ]
    counted = len(read)
    narrowed = sum(row.executed_kind is EnumSelectionKind.NARROWED for row in read)
    selected = sum(row.selected_seconds or 0.0 for row in read)
    full = sum(row.full_suite_seconds or 0.0 for row in read)
    saved = sum(row.seconds_saved or 0.0 for row in read)
    minutes = sum(row.ci_minutes or 0.0 for row in read)
    why = Counter(
        row.unreadable_reason for row in rows if row.unreadable_reason is not None
    )
    return ModelStageSavingsTotals(
        prs_in_scope=len(rows),
        prs_counted=counted,
        prs_unreadable=len(rows) - counted,
        prs_narrowed=narrowed,
        prs_by_selection_stage=dict(
            sorted(
                Counter(
                    row.selection_stage
                    for row in read
                    if row.selection_stage is not None
                ).items()
            )
        ),
        merge_narrowed_share=round(narrowed / counted, _PLACES) if counted else None,
        selected_seconds_total=round(selected, _PLACES),
        full_suite_seconds_total=round(full, _PLACES),
        seconds_saved_total=round(saved, _PLACES),
        seconds_saved_share=round(saved / full, _PLACES) if full else None,
        test_seconds_per_merge=round(selected / counted, _PLACES) if counted else None,
        ci_minutes_total=round(minutes, _PLACES),
        ci_minutes_per_pr=round(minutes / counted, _PLACES) if counted else None,
        unreadable_by_reason={reason: why[reason] for reason in _Reason if why[reason]},
    )
