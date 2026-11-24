# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""report_stage_savings: what the selection of each merged pull request saved against the full suite.

Golden chain: three pull requests since the stage went live (two narrowed, one full) are read through the
gateway twin and the totals are the sums over the read rows. Error chain: a record whose selected seconds
exceed the full-suite seconds, a pull request with no record, and every other unreadable read is a row with a
typed reason and is counted in no total.
"""

from __future__ import annotations

import importlib
import io
import zipfile
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.handlers.adapter_test_selection_policy_file import load_policy
from omnimarket.models.model_gateway_node_skew import GatewayReadError
from omnimarket.models.model_github_gateway_io import (
    ModelPrHead,
    ModelWorkflowRunSummary,
)
from omnimarket.nodes.node_test_selection_selector_compute.adapters.gateway_pr_records_in_memory import (
    GatewayPrRecordsInMemory,
)
from omnimarket.nodes.node_test_selection_selector_compute.adapters.source_in_memory import (
    SourceInMemory,
)
from omnimarket.nodes.node_test_selection_selector_compute.adapters.source_test_files import (
    SourceTestFilesInMemory,
)
from omnimarket.nodes.node_test_selection_selector_compute.handlers.handler_stage_savings_report import (
    HandlerStageSavingsReport,
)
from omnimarket.nodes.node_test_selection_selector_compute.handlers.handler_test_selection_selector import (
    HandlerTestSelectionSelector,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_stage_savings_report import (
    EnumSavingsRowStatus,
    EnumSavingsUnreadableReason,
    ModelStageSavingsReport,
    ModelStageSavingsReportRequest,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_test_selection_record import (
    EnumSelectionKind,
    ModelStageSelection,
    ModelTestSelectionRecord,
)

pytestmark = pytest.mark.unit

NODE_DIR = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_test_selection_selector_compute"
)
REPO = "OmniNode-ai/omnimarket"
Reason = EnumSavingsUnreadableReason
#: Two narrowed paths and the whole suite: the suite costs 100 + 300 + 600 = 1000 recorded seconds.
DURATIONS = {
    "tests/a/test_a.py": 100.0,
    "tests/b/test_b.py": 300.0,
    "tests/c/test_c.py": 600.0,
}
FIRST_PR = 100
FIRST_MERGED = "2026-11-23T12:00:00Z"


def _sha(n: int) -> str:
    return f"{n:040x}"


def _entry(
    kind: EnumSelectionKind, seconds: float | None, paths: list[str]
) -> ModelStageSelection:
    full = kind is EnumSelectionKind.FULL_SUITE
    return ModelStageSelection(
        kind=kind,
        reason_code="shared_module" if full else None,
        selected_paths=paths,
        split_count=20 if full else 1,
        selected_seconds=seconds,
        selector_version="1.0.0",
        policy_version="p" * 12,
    )


def _record(
    pr: int,
    run_id: int,
    attempt: int = 1,
    *,
    kind: EnumSelectionKind = EnumSelectionKind.NARROWED,
    seconds: float | None = 100.0,
    head: str | None = None,
) -> ModelTestSelectionRecord:
    paths = ["tests/"] if kind is EnumSelectionKind.FULL_SUITE else ["tests/a/"]
    entry = _entry(kind, seconds, paths)
    return ModelTestSelectionRecord(
        repo=REPO,
        pr_number=pr,
        head_sha=head or _sha(pr),
        base_sha=_sha(1),
        checkout_sha=_sha(2),
        run_id=run_id,
        run_attempt=attempt,
        changed_files=("src/x.py",),
        policy_version="p" * 12,
        selection_stage=1,
        executed_selection=entry,
        selection_by_stage={0: entry, 1: entry},
        graph_source="none",
    )


def _zip(body: str, name: str = "test-selection.json") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(name, body)
    return buffer.getvalue()


class _World:
    """A repository the twin holds: pull requests, their CI runs and the record artifacts of those runs."""

    def __init__(self) -> None:
        self.gateway = GatewayPrRecordsInMemory()
        self.gateway.add_pr(
            ModelPrHead(
                number=FIRST_PR,
                state="MERGED",
                head_oid=_sha(FIRST_PR),
                base_ref="dev",
                merged_at=FIRST_MERGED,
            )
        )

    def pr(
        self,
        number: int,
        *,
        state: str = "MERGED",
        merged_at: str = "2026-11-23T13:00:00Z",
    ) -> None:
        self.gateway.add_pr(
            ModelPrHead(
                number=number,
                state=state,
                head_oid=_sha(number),
                base_ref="dev",
                merged_at=merged_at if state == "MERGED" else None,
            )
        )

    def run(
        self,
        pr: int,
        run_id: int,
        *,
        minutes: int = 30,
        status: str = "completed",
        attempt: int = 1,
    ) -> None:
        end = f"2026-11-23T14:{minutes:02d}:00Z"
        self.gateway.add_run(
            ModelWorkflowRunSummary(
                id=run_id,
                name="CI",
                workflow_id=1,
                head_sha=_sha(pr),
                event="pull_request",
                status=status,
                conclusion="success" if status == "completed" else None,
                run_number=run_id,
                run_attempt=attempt,
                created_at="2026-11-23T14:00:00Z",
                updated_at=end,
                html_url="https://example.invalid/run",
            )
        )

    def record(
        self,
        pr: int,
        run_id: int,
        attempt: int = 1,
        *,
        kind: EnumSelectionKind = EnumSelectionKind.NARROWED,
        seconds: float | None = 100.0,
        head: str | None = None,
        name: str | None = None,
        expired: bool = False,
    ) -> None:
        record = _record(pr, run_id, attempt, kind=kind, seconds=seconds, head=head)
        self.gateway.add_artifact(
            run_id,
            name or f"test-selection-{pr}-{run_id}-{attempt}",
            _zip(record.model_dump_json()),
            expired=expired,
        )

    def merged(
        self, pr: int, run_id: int, **record: Any
    ) -> None:  # a merged pull request with a CI run and its record
        self.pr(pr)
        self.run(pr, run_id)
        self.record(pr, run_id, **record)

    def handler(self) -> HandlerStageSavingsReport:
        policy = load_policy()
        selector = HandlerTestSelectionSelector(
            SourceInMemory(DURATIONS),
            policy,
            SourceTestFilesInMemory(tuple(DURATIONS)),
        )
        return HandlerStageSavingsReport(self.gateway, policy, selector)

    def report(self) -> ModelStageSavingsReport:
        return self.handler().handle(
            ModelStageSavingsReportRequest(
                repository=REPO, repo_root=".", first_pr_number=FIRST_PR
            )
        )


def _row(report: ModelStageSavingsReport, pr: int) -> Any:
    return next(row for row in report.rows if row.pr_number == pr)


def test_the_report_totals_the_pull_requests_merged_since_the_stage_went_live() -> None:
    world = _World()
    world.run(FIRST_PR, 9100)
    world.record(FIRST_PR, 9100, seconds=100.0)  # the first pull request: 100 of 1000
    world.merged(101, 9101, seconds=300.0)  # narrowed: 300 of 1000
    world.merged(102, 9102, kind=EnumSelectionKind.FULL_SUITE, seconds=1000.0)
    world.pr(99, merged_at="2026-11-23T11:00:00Z")  # merged before: not in scope
    world.run(99, 9099)
    world.record(99, 9099)
    world.pr(103, state="OPEN")  # not merged: not in scope
    report = world.report()
    totals = report.totals
    assert report.full_suite_seconds == 1000.0
    assert [row.pr_number for row in report.rows] == [100, 101, 102]
    assert totals.prs_in_scope == 3
    assert totals.prs_counted == 3
    assert totals.prs_unreadable == 0
    assert totals.prs_narrowed == 2
    assert totals.merge_narrowed_share == pytest.approx(2 / 3, abs=1e-6)
    assert totals.selected_seconds_total == 1400.0
    assert totals.full_suite_seconds_total == 3000.0
    assert totals.seconds_saved_total == 1600.0
    assert totals.seconds_saved_share == pytest.approx(1600 / 3000, abs=1e-6)
    assert totals.test_seconds_per_merge == pytest.approx(1400 / 3, abs=1e-6)
    assert totals.ci_minutes_total == 90.0
    assert totals.ci_minutes_per_pr == 30.0
    assert totals.unreadable_by_reason == {}
    assert totals.prs_by_selection_stage == {1: 3}
    row = _row(report, 101)
    assert (row.selected_seconds, row.full_suite_seconds, row.seconds_saved) == (
        300.0,
        1000.0,
        700.0,
    )
    assert (row.run_id, row.run_attempt, row.ci_minutes) == (9101, 1, 30.0)


def test_a_record_whose_selected_seconds_exceed_the_full_suite_is_refused_not_averaged() -> (
    None
):
    world = _World()
    world.run(FIRST_PR, 9100)
    world.record(FIRST_PR, 9100, seconds=100.0)
    world.merged(101, 9101, seconds=1500.0)  # 1500 selected of a 1000-second suite
    report = world.report()
    row = _row(report, 101)
    assert row.status is EnumSavingsRowStatus.UNREADABLE
    assert row.unreadable_reason is Reason.SELECTED_EXCEEDS_FULL
    assert row.selected_seconds is None
    assert report.totals.prs_counted == 1
    assert report.totals.prs_unreadable == 1
    assert report.totals.selected_seconds_total == 100.0
    assert report.totals.full_suite_seconds_total == 1000.0
    assert report.totals.unreadable_by_reason == {Reason.SELECTED_EXCEEDS_FULL: 1}


def test_a_full_suite_run_saves_nothing_whatever_its_own_estimate_says() -> None:
    world = _World()
    world.run(FIRST_PR, 9100)
    world.record(FIRST_PR, 9100, kind=EnumSelectionKind.FULL_SUITE, seconds=950.0)
    world.merged(101, 9101, kind=EnumSelectionKind.FULL_SUITE, seconds=1030.0)
    world.merged(102, 9102, kind=EnumSelectionKind.FULL_SUITE, seconds=1100.0)
    report = world.report()
    for pr in (100, 101):
        row = _row(report, pr)
        assert (row.selected_seconds, row.seconds_saved) == (1000.0, 0.0)
    assert _row(report, 100).recorded_selected_seconds == 950.0
    assert _row(report, 101).recorded_selected_seconds == 1030.0
    assert _row(report, 102).unreadable_reason is Reason.SELECTED_EXCEEDS_FULL
    assert report.totals.seconds_saved_total == 0.0
    assert (
        report.totals.selected_seconds_total == report.totals.full_suite_seconds_total
    )
    assert report.totals.prs_narrowed == 0


def test_a_narrowed_record_over_the_full_suite_by_a_hair_is_still_refused() -> None:
    world = _World()
    world.run(FIRST_PR, 9100)
    world.record(FIRST_PR, 9100, seconds=1000.5)
    assert _row(world.report(), 100).unreadable_reason is Reason.SELECTED_EXCEEDS_FULL


def test_the_counted_pull_requests_are_split_by_the_stage_their_record_was_built_under() -> (
    None
):
    world = _World()
    world.run(FIRST_PR, 9100)
    world.record(FIRST_PR, 9100)
    world.merged(101, 9101)
    # a record built before the stage went live holds the older stage
    old = _record(102, 9102).model_copy(update={"selection_stage": 0})
    world.pr(102)
    world.run(102, 9102)
    world.gateway.add_artifact(
        9102, "test-selection-102-9102-1", _zip(old.model_dump_json())
    )
    assert world.report().totals.prs_by_selection_stage == {0: 1, 1: 2}


def test_a_pull_request_with_no_record_is_counted_unreadable_and_has_no_figure() -> (
    None
):
    world = _World()
    world.run(FIRST_PR, 9100)
    world.record(FIRST_PR, 9100, seconds=100.0)
    world.pr(101)
    world.run(101, 9101)  # a run, and no record artifact in it
    world.pr(102)  # no run at all
    report = world.report()
    assert _row(report, 101).unreadable_reason is Reason.NO_RECORD
    assert _row(report, 102).unreadable_reason is Reason.NO_RUN
    assert report.totals.prs_in_scope == 3
    assert report.totals.prs_counted == 1
    assert report.totals.prs_unreadable == 2
    assert report.totals.ci_minutes_total == 30.0
    assert report.totals.ci_minutes_per_pr == 30.0
    assert report.totals.merge_narrowed_share == 1.0


def test_every_other_unreadable_read_is_a_typed_row() -> None:
    world = _World()
    world.run(FIRST_PR, 9100)
    world.record(FIRST_PR, 9100, seconds=100.0)
    world.pr(101)
    world.run(101, 9101)
    world.record(101, 9101, expired=True)
    world.pr(102)
    world.run(102, 9102)
    world.gateway.add_artifact(9102, "test-selection-102-9102-1", _zip("not json"))
    world.pr(103)
    world.run(103, 9103)
    world.record(103, 9103, head=_sha(7))  # a record for another head commit
    world.pr(104)
    world.run(104, 9104)
    world.record(104, 9104, seconds=None)
    world.pr(105)
    world.run(105, 9105, status="in_progress")
    world.record(105, 9105)
    world.pr(106)
    world.run(106, 9106)
    world.gateway.add_artifact(9106, "test-selection-106-9106-1", b"not a zip")
    world.pr(107)
    world.run(107, 9107)
    world.gateway.add_artifact(9107, "test-selection-107-9107-1", _zip("{}", "a.txt"))
    report = world.report()
    reasons = {row.pr_number: row.unreadable_reason for row in report.rows}
    assert reasons == {
        100: None,
        101: Reason.RECORD_EXPIRED,
        102: Reason.RECORD_UNPARSEABLE,
        103: Reason.RECORD_IDENTITY_MISMATCH,
        104: Reason.SELECTED_SECONDS_UNREAD,
        105: Reason.RUN_UNFINISHED,
        106: Reason.RECORD_UNPARSEABLE,
        107: Reason.RECORD_UNPARSEABLE,
    }
    assert report.totals.prs_counted == 1
    assert report.totals.prs_unreadable == 7


def test_an_artifact_over_the_size_cap_is_refused() -> None:
    world = _World()
    world.run(FIRST_PR, 9100)
    world.record(FIRST_PR, 9100)
    world.pr(101)
    world.run(101, 9101)
    world.gateway.add_artifact(9101, "test-selection-101-9101-1", _zip("x" * 200_000))
    policy = load_policy().model_copy(update={"savings_report_artifact_max_bytes": 100})
    handler = HandlerStageSavingsReport(
        world.gateway,
        policy,
        HandlerTestSelectionSelector(
            SourceInMemory(DURATIONS), policy, SourceTestFilesInMemory(tuple(DURATIONS))
        ),
    )
    report = handler.handle(
        ModelStageSavingsReportRequest(
            repository=REPO, repo_root=".", first_pr_number=FIRST_PR
        )
    )
    assert {row.pr_number: row.unreadable_reason for row in report.rows} == {
        100: Reason.RECORD_REFUSED,
        101: Reason.RECORD_REFUSED,
    }
    assert report.totals.prs_counted == 0
    assert report.totals.merge_narrowed_share is None
    assert report.totals.ci_minutes_per_pr is None


def test_the_latest_attempt_of_the_latest_run_is_the_record() -> None:
    world = _World()
    world.run(FIRST_PR, 9100)
    world.record(FIRST_PR, 9100, seconds=100.0)
    world.pr(101)
    world.run(101, 9101, attempt=2)
    world.record(101, 9101, 1, seconds=900.0)
    world.record(101, 9101, 2, seconds=200.0)
    row = _row(world.report(), 101)
    assert (row.run_attempt, row.selected_seconds) == (2, 200.0)


def test_a_gateway_that_gives_no_answer_for_one_pull_request_leaves_that_row_unreadable() -> (
    None
):
    world = _World()
    world.run(FIRST_PR, 9100)
    world.record(FIRST_PR, 9100)
    world.merged(101, 9101)
    world.gateway.unreachable_heads = frozenset({_sha(101)})
    report = world.report()
    assert _row(report, 101).unreadable_reason is Reason.GATEWAY_UNREADABLE
    assert report.totals.prs_counted == 1


def test_a_gateway_that_gives_no_answer_for_the_list_is_an_error_not_an_empty_report() -> (
    None
):
    world = _World()
    world.gateway.unreachable = True
    with pytest.raises(GatewayReadError):
        world.report()


def test_a_first_pull_request_outside_the_listed_ones_is_refused() -> None:
    world = _World()
    world.gateway.heads_truncated = True
    handler = world.handler()
    with pytest.raises(ValueError, match="not among"):
        handler.handle(
            ModelStageSavingsReportRequest(
                repository=REPO, repo_root=".", first_pr_number=5
            )
        )


def test_a_policy_without_the_report_keys_cannot_report() -> None:
    world = _World()
    policy = load_policy().model_copy(update={"savings_report_first_pr": None})
    with pytest.raises(ValueError, match="savings_report"):
        HandlerStageSavingsReport(world.gateway, policy)


def test_the_policy_names_the_first_pull_request_the_workflow_and_the_bounds() -> None:
    first, workflow, limit, cap = load_policy().savings_report_settings()
    assert (first, workflow) == (6634, "ci.yml")
    assert 1 <= limit <= 300
    assert 1 <= cap <= 524288


def test_the_contract_routes_the_operation_to_the_handler_and_its_models() -> None:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())
    entries = [
        entry
        for entry in contract["handler_routing"]["handlers"]
        if entry["operation"] == "report_stage_savings"
    ]
    assert len(entries) == 1
    entry = entries[0]
    for key in ("input_model", "output_model"):
        module, _, name = entry[key].rpartition(".")
        assert hasattr(importlib.import_module(module), name)
    module = importlib.import_module(entry["handler"]["module"])
    assert getattr(module, entry["handler"]["name"]) is HandlerStageSavingsReport
