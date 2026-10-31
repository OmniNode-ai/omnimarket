# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_receipt_gate_pin_friction_report_compute: the comparison per pin kind over a window [OMN-20134].

Rows are planted for both pin kinds so each measure the ruling asked for has a
known answer: PRs opened, merged untouched, median time to merge, touches per PR
and refusals.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from omnimarket.models.receipt_gate_pin_friction import (
    EnumPinFrictionReason,
    EnumPinKind,
    EnumPinPrState,
    EnumPinRefusalClass,
    ModelPinFrictionRow,
    ModelPinRefusal,
)
from omnimarket.nodes.node_receipt_gate_pin_friction_report_compute.handlers.handler_pin_friction_report import (
    HandlerReceiptGatePinFrictionReport,
)
from omnimarket.nodes.node_receipt_gate_pin_friction_report_compute.models import (
    ModelPinFrictionReport,
    ModelPinFrictionReportRequest,
    ModelPinKindSummary,
)

pytestmark = pytest.mark.unit

START = datetime(2026, 10, 1, tzinfo=UTC)
END = datetime(2026, 11, 1, tzinfo=UTC)
TAG = "dev-20261030152335-66d35d2283c3"


def _row(
    n: int,
    kind: EnumPinKind,
    *,
    state: EnumPinPrState = EnumPinPrState.MERGED,
    seen: datetime = START + timedelta(days=3),
    open_to_merge: float | None = 600.0,
    fix_pushes: int = 0,
    reruns: int = 0,
    refusals: tuple[EnumPinRefusalClass, ...] = (),
    friction: tuple[EnumPinFrictionReason, ...] = (),
) -> ModelPinFrictionRow:
    opened = state in {
        EnumPinPrState.OPENED,
        EnumPinPrState.MERGED,
        EnumPinPrState.CLOSED,
    }
    return ModelPinFrictionRow(
        repository=f"OmniNode-ai/repo{n}",
        branch=f"pin-advance/receipt-gate/{TAG}",
        pin_kind=kind,
        core_tag=TAG,
        state=state,
        first_seen_at=seen,
        pr_number=n if opened else None,
        tag_pushed_at=seen - timedelta(seconds=60),
        pr_opened_at=seen if opened else None,
        merged_at=(
            seen + timedelta(seconds=open_to_merge or 0)
            if state is EnumPinPrState.MERGED
            else None
        ),
        seconds_pr_open_to_merge=(
            open_to_merge if state is EnumPinPrState.MERGED else None
        ),
        seconds_tag_to_merge=(
            open_to_merge + 60
            if state is EnumPinPrState.MERGED and open_to_merge is not None
            else None
        ),
        head_shas=tuple(f"{i:040x}" for i in range(fix_pushes + 1)) if opened else (),
        rerun_keys=tuple(f"{n}:{i + 2}" for i in range(reruns)),
        refusals=tuple(
            ModelPinRefusal(refusal_class=c, detail="planted", at=seen)
            for c in refusals
        ),
        friction_reasons=friction,
        last_event_at=seen,
    )


def _report(
    *rows: ModelPinFrictionRow, start: datetime = START, end: datetime = END
) -> ModelPinFrictionReport:
    return HandlerReceiptGatePinFrictionReport().handle(
        ModelPinFrictionReportRequest(rows=rows, window_start=start, window_end=end)
    )


def _kind(report: ModelPinFrictionReport, kind: EnumPinKind) -> ModelPinKindSummary:
    return next(k for k in report.kinds if k.pin_kind is kind)


def test_each_measure_is_counted_per_pin_kind_over_the_planted_rows() -> None:
    report = _report(
        # sha: 3 opened, 2 merged (one untouched), 1 still open; one CI re-run and one fix push
        _row(1, EnumPinKind.SHA, open_to_merge=600.0),
        _row(
            2,
            EnumPinKind.SHA,
            open_to_merge=1800.0,
            fix_pushes=1,
            reruns=1,
            refusals=(EnumPinRefusalClass.AUDIT,),
            friction=(EnumPinFrictionReason.HUMAN_TOUCHED,),
        ),
        _row(3, EnumPinKind.SHA, state=EnumPinPrState.OPENED),
        # tag: 4 opened and merged untouched, one updater failure (never opened)
        _row(4, EnumPinKind.TAG, open_to_merge=300.0),
        _row(5, EnumPinKind.TAG, open_to_merge=420.0),
        _row(6, EnumPinKind.TAG, open_to_merge=540.0),
        _row(7, EnumPinKind.TAG, open_to_merge=1200.0),
        _row(
            8,
            EnumPinKind.TAG,
            state=EnumPinPrState.FAILED,
            refusals=(EnumPinRefusalClass.GUARD, EnumPinRefusalClass.CONFLICT),
            friction=(EnumPinFrictionReason.UPDATER_FAILED,),
        ),
    )
    sha = _kind(report, EnumPinKind.SHA)
    tag = _kind(report, EnumPinKind.TAG)
    assert report.rows_in_window == 8

    assert (sha.prs_opened, sha.merged, sha.merged_untouched, sha.still_open) == (
        3,
        2,
        1,
        1,
    )
    assert sha.untouched_rate == pytest.approx(1 / 3)
    assert sha.median_seconds_pr_open_to_merge == 1200.0
    assert sha.median_seconds_tag_to_merge == 1260.0
    assert (sha.fix_pushes, sha.ci_reruns) == (1, 1)
    assert sha.touches_per_pr == pytest.approx(2 / 3)
    assert sha.refusals == 1
    assert [(r.refusal_class, r.count) for r in sha.refusals_by_class] == [
        (EnumPinRefusalClass.AUDIT, 1)
    ]
    assert sha.updater_failures == 0

    assert (tag.prs_opened, tag.merged, tag.merged_untouched, tag.still_open) == (
        4,
        4,
        4,
        0,
    )
    assert tag.untouched_rate == 1.0
    assert tag.median_seconds_pr_open_to_merge == 480.0
    assert tag.touches_per_pr == 0.0
    assert tag.refusals == 2
    assert {r.refusal_class for r in tag.refusals_by_class} == {
        EnumPinRefusalClass.GUARD,
        EnumPinRefusalClass.CONFLICT,
    }
    assert tag.updater_failures == 1


def test_only_rows_first_seen_inside_the_window_count() -> None:
    report = _report(
        _row(1, EnumPinKind.TAG, seen=START - timedelta(seconds=1)),
        _row(2, EnumPinKind.TAG, seen=START),
        _row(3, EnumPinKind.TAG, seen=END - timedelta(seconds=1)),
        _row(4, EnumPinKind.TAG, seen=END),
    )
    assert report.rows_in_window == 2
    assert _kind(report, EnumPinKind.TAG).prs_opened == 2


def test_a_pin_kind_with_no_rows_reports_zero_and_no_rates() -> None:
    report = _report(_row(1, EnumPinKind.TAG))
    sha = _kind(report, EnumPinKind.SHA)
    assert (sha.prs_opened, sha.merged, sha.refusals, sha.updater_failures) == (
        0,
        0,
        0,
        0,
    )
    assert sha.untouched_rate is None
    assert sha.touches_per_pr is None
    assert sha.median_seconds_pr_open_to_merge is None


def test_a_closed_pull_request_is_opened_but_not_merged() -> None:
    report = _report(_row(1, EnumPinKind.SHA, state=EnumPinPrState.CLOSED))
    sha = _kind(report, EnumPinKind.SHA)
    assert (sha.prs_opened, sha.merged, sha.closed_unmerged) == (1, 0, 1)


def test_the_window_must_be_ordered() -> None:
    with pytest.raises(ValueError, match="window_end must be after window_start"):
        _report(start=END, end=START)


def test_rows_read_back_from_the_projection_as_json_validate_and_render_a_table() -> (
    None
):
    raw = [
        _row(1, EnumPinKind.SHA, fix_pushes=1).model_dump(mode="json")
        | {"updated_at": "2026-10-31T00:00:00Z", "projection_cursor": 12},
        _row(2, EnumPinKind.TAG).model_dump(mode="json"),
    ]
    request = ModelPinFrictionReportRequest.model_validate(
        {
            "rows": raw,
            "window_start": START.isoformat(),
            "window_end": END.isoformat(),
        }
    )
    table = HandlerReceiptGatePinFrictionReport().handle(request).render()
    lines = table.splitlines()
    assert lines[0].startswith(
        "pin friction 2026-10-01T00:00:00Z .. 2026-11-01T00:00:00Z"
    )
    assert lines[1].split() == ["measure", "sha", "tag"]
    by_label = {line[:26].strip(): line[26:].split() for line in lines[2:]}
    assert by_label["PRs opened"] == ["1", "1"]
    assert by_label["merged untouched"] == ["0", "1"]
    assert by_label["untouched rate"] == ["0%", "100%"]
    assert by_label["touches per PR"] == ["1.00", "0.00"]
    assert by_label["median open to merge"] == ["10.0m", "10.0m"]
    assert "audit" in by_label
