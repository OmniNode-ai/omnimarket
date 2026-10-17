# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain: readings -> verdict per indicator -> alert actions -> board (OMN-17427).

The readings are the failures of the 2026-10-16/17 night that no check warned
about: 6 of 48 slots used while work waited, every lane no-host after a parity
fix, the omnibase_infra merge-queue head failing for 5 h, green PRs unlanded
for 2-4 h, merged plugin fixes not installed for hours, scheduled jobs failing
daily, an 19.7 GB fseventsd store and hook-preflight refusing fix lanes.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from omnimarket.enums.enum_lab_sli import (
    EnumLabSliActionKind,
    EnumLabSliSeverity,
    EnumLabSliStatus,
)
from omnimarket.models.lab_sli import (
    ModelLabSliAlertRequest,
    ModelLabSliBoardRequest,
    ModelLabSliReading,
    ModelLabSliReadingBatch,
    ModelLabSliSubjectReading,
)
from omnimarket.nodes.node_lab_sli_alert_effect.handlers.handler_lab_sli_alert import (
    HandlerLabSliAlert,
)
from omnimarket.nodes.node_lab_sli_board_compute.handlers.handler_lab_sli_board import (
    HandlerLabSliBoard,
)
from omnimarket.nodes.node_lab_sli_compute.handlers.handler_lab_sli_evaluate import (
    HandlerLabSliEvaluate,
    load_sli_table,
)

pytestmark = pytest.mark.unit

T0 = datetime(2026, 10, 17, 3, 0, tzinfo=UTC)


def _reading(sli_id: str, **subjects: float) -> ModelLabSliReading:
    return ModelLabSliReading(
        sli_id=sli_id,
        subjects=tuple(
            ModelLabSliSubjectReading(subject=name, value=value)
            for name, value in subjects.items()
        ),
    )


def _night(start: datetime = T0) -> ModelLabSliReadingBatch:
    return ModelLabSliReadingBatch(
        interval_start=start,
        collected_at=start + timedelta(minutes=1),
        collector_host="h101",
        readings=(
            _reading("slot_utilization_while_queued", lab=6 / 48),
            _reading("lane_placement_no_host_rate", lab=1.0),
            _reading("merge_queue_head_failure_streak", omnibase_infra=6, omnimarket=0),
            _reading("oldest_green_unlanded_minutes", **{"omnimarket#4004": 149.0}),
            _reading("merged_to_running_lag_minutes", h101=200.0, h201=5.0),
            _reading("scheduled_job_failures", lab=3),
            _reading("host_free_memory_gb", h101=61.0),
            _reading("host_fseventsd_gb", h101=19.7),
            _reading("push_refusal_rate", lab=0.6),
            ModelLabSliReading(sli_id="merges_per_hour_per_repo", applicable=False),
        ),
    )


def test_every_indicator_in_the_table_gets_exactly_one_verdict() -> None:
    batch = HandlerLabSliEvaluate().handle(_night())
    table = load_sli_table()
    assert [e.sli_id for e in batch.evaluations] == [d.sli_id for d in table]
    assert len({d.severity for d in table}) == 4, "the table uses all four severities"


def test_the_night_breaches_carry_their_own_severity() -> None:
    by_id = {e.sli_id: e for e in HandlerLabSliEvaluate().handle(_night()).evaluations}
    breach = {k for k, e in by_id.items() if e.status is EnumLabSliStatus.BREACH}
    assert breach == {
        "slot_utilization_while_queued",
        "lane_placement_no_host_rate",
        "merge_queue_head_failure_streak",
        "oldest_green_unlanded_minutes",
        "merged_to_running_lag_minutes",
        "scheduled_job_failures",
        "host_fseventsd_gb",
        "push_refusal_rate",
        "sli_source_coverage",
    }
    assert by_id["lane_placement_no_host_rate"].severity is EnumLabSliSeverity.SEV1
    assert by_id["merge_queue_head_failure_streak"].worst_subject == "omnibase_infra"
    assert by_id["merge_queue_head_failure_streak"].breaching_subjects == (
        "omnibase_infra",
    )
    assert by_id["merged_to_running_lag_minutes"].worst_subject == "h101"
    # 61 GB free is healthy: the indicator is ok, it is the pressure gate that was wrong.
    assert by_id["host_free_memory_gb"].status is EnumLabSliStatus.OK
    assert by_id["merges_per_hour_per_repo"].status is EnumLabSliStatus.NOT_APPLICABLE
    # No reading is never ok.
    assert by_id["host_cpu_temperature_c"].status is EnumLabSliStatus.UNKNOWN
    assert by_id["host_cpu_temperature_c"].value is None


def test_source_coverage_is_computed_from_the_other_verdicts() -> None:
    by_id = {e.sli_id: e for e in HandlerLabSliEvaluate().handle(_night()).evaluations}
    coverage = by_id["sli_source_coverage"]
    # 16 measured indicators; 10 read (merges_per_hour not applicable counts as read).
    assert coverage.value == pytest.approx(10 / 16)
    assert coverage.severity is EnumLabSliSeverity.SEV4
    assert "green_to_merged_minutes" in coverage.detail


def test_alerts_act_per_severity_and_batch_the_drift() -> None:
    evaluation = HandlerLabSliEvaluate().handle(_night())
    result = HandlerLabSliAlert().handle(ModelLabSliAlertRequest(evaluation=evaluation))
    kinds = [(a.kind, a.sli_ids) for a in result.actions]
    pushes = [a for a in result.actions if a.kind is EnumLabSliActionKind.PUSH_OPERATOR]
    assert {a.sli_ids for a in pushes} == {
        ("lane_placement_no_host_rate",),
        ("merge_queue_head_failure_streak",),
    }
    fix = {
        a.sli_ids[0] for a in result.actions if a.kind is EnumLabSliActionKind.FIX_LANE
    }
    assert fix == {
        "lane_placement_no_host_rate",
        "merge_queue_head_failure_streak",
        "slot_utilization_while_queued",
        "oldest_green_unlanded_minutes",
        "scheduled_job_failures",
        "push_refusal_rate",
    }
    batched = [
        a for a in result.actions if a.kind is EnumLabSliActionKind.BATCHED_FIX_LANE
    ]
    assert len(batched) == 1, kinds
    # Drift breaches and dark sources ride one lane.
    assert "merged_to_running_lag_minutes" in batched[0].sli_ids
    assert "host_fseventsd_gb" in batched[0].sli_ids
    assert "host_cpu_temperature_c" in batched[0].sli_ids
    # SEV4 never acts.
    assert all("sli_source_coverage" not in a.sli_ids for a in result.actions)
    sev1_lane = next(
        a
        for a in result.actions
        if a.kind is EnumLabSliActionKind.FIX_LANE
        and a.sli_ids == ("lane_placement_no_host_rate",)
    )
    assert sev1_lane.lane.startswith("lab-sli-fix-lane-placement-no-host-rate-")
    assert "Placement fix lane" in sev1_lane.text


def test_a_still_firing_indicator_is_deduplicated_until_renotify() -> None:
    handler = HandlerLabSliAlert()
    first = handler.handle(
        ModelLabSliAlertRequest(evaluation=HandlerLabSliEvaluate().handle(_night()))
    )
    later = T0 + timedelta(minutes=15)
    second = handler.handle(
        ModelLabSliAlertRequest(
            evaluation=HandlerLabSliEvaluate().handle(_night(later)),
            state=first.state,
        )
    )
    assert second.actions == ()
    assert "lane_placement_no_host_rate" in second.suppressed
    much_later = T0 + timedelta(minutes=61)
    third = handler.handle(
        ModelLabSliAlertRequest(
            evaluation=HandlerLabSliEvaluate().handle(_night(much_later)),
            state=second.state,
        )
    )
    renotified = {a.sli_ids for a in third.actions}
    # SEV1 renotifies after 60 minutes; SEV2 and SEV3 have not reached theirs.
    assert ("lane_placement_no_host_rate",) in renotified
    assert ("push_refusal_rate",) not in renotified


def test_a_recovered_indicator_clears_and_fires_again_at_once() -> None:
    handler = HandlerLabSliAlert()
    first = handler.handle(
        ModelLabSliAlertRequest(evaluation=HandlerLabSliEvaluate().handle(_night()))
    )
    healthy = ModelLabSliReadingBatch(
        interval_start=T0 + timedelta(minutes=15),
        collected_at=T0 + timedelta(minutes=16),
        collector_host="h101",
        readings=(_reading("lane_placement_no_host_rate", lab=0.0),),
    )
    cleared = handler.handle(
        ModelLabSliAlertRequest(
            evaluation=HandlerLabSliEvaluate().handle(healthy), state=first.state
        )
    )
    assert "lane_placement_no_host_rate" in cleared.cleared
    assert "lane_placement_no_host_rate" not in cleared.state.firing
    again = handler.handle(
        ModelLabSliAlertRequest(
            evaluation=HandlerLabSliEvaluate().handle(
                _night(T0 + timedelta(minutes=30))
            ),
            state=cleared.state,
        )
    )
    assert ("lane_placement_no_host_rate",) in {a.sli_ids for a in again.actions}


def test_the_board_keeps_history_and_renders_each_indicator_with_trend() -> None:
    board_handler = HandlerLabSliBoard()
    projection = None
    for step in range(3):
        evaluation = HandlerLabSliEvaluate().handle(
            _night(T0 + timedelta(minutes=15 * step))
        )
        request = (
            ModelLabSliBoardRequest(evaluation=evaluation)
            if projection is None
            else ModelLabSliBoardRequest(evaluation=evaluation, projection=projection)
        )
        board = board_handler.handle(request)
        projection = board.projection
    assert projection is not None
    no_host = [
        p for p in projection.points if p.sli_id == "lane_placement_no_host_rate"
    ]
    assert len(no_host) == 3
    assert len(projection.latest) == len(load_sli_table())
    for definition in load_sli_table():
        assert definition.sli_id in board.html
        assert definition.sli_id in board.markdown
    assert "SEV1" in board.html
    assert "breach" in board.html
    # SEV1 rows sort first.
    assert board.markdown.index("lane_placement_no_host_rate") < board.markdown.index(
        "host_fseventsd_gb"
    )


def test_actions_reach_the_push_and_fix_lane_command_topics() -> None:
    import json

    from omnimarket.models.lab_sli import ModelLabSliFixLaneRequested

    sent: list[tuple[str, bytes]] = []
    handler = HandlerLabSliAlert(
        event_publisher=lambda topic, body: sent.append((topic, body)),
        channel_resolver=lambda: "C-TEST",
    )
    result = handler.handle(
        ModelLabSliAlertRequest(evaluation=HandlerLabSliEvaluate().handle(_night()))
    )
    assert len(sent) == 2
    assert len(result.deliveries) == 2
    assert all(d.published for d in result.deliveries)
    pushes = [
        json.loads(b) for t, b in sent if t == "onex.cmd.omnimarket.slack-publish.v1"
    ]
    assert {p["channel"] for p in pushes} == {"C-TEST"}
    assert len(pushes) == 2
    assert all(p["text"].startswith("SEV1 ") for p in pushes)
    assert {t for t, _ in sent} == {"onex.cmd.omnimarket.slack-publish.v1"}
    # Six fix lanes and one batched lane ride on the terminal event.
    lanes = [
        ModelLabSliFixLaneRequested.model_validate_json(lane.model_dump_json())
        for lane in result.fix_lanes
    ]
    assert len(lanes) == 7
    assert len({lane.idempotency_key for lane in lanes}) == 7


def test_a_failed_delivery_is_recorded_and_the_rest_still_go_out() -> None:
    sent: list[str] = []

    def publisher(topic: str, body: bytes) -> None:
        if topic.endswith(".slack-publish.v1"):
            raise ConnectionError("broker down")
        sent.append(topic)

    result = HandlerLabSliAlert(
        event_publisher=publisher, channel_resolver=lambda: "C-TEST"
    ).handle(
        ModelLabSliAlertRequest(evaluation=HandlerLabSliEvaluate().handle(_night()))
    )
    failed = [d for d in result.deliveries if not d.published]
    assert len(failed) == 2
    assert all(d.error == "ConnectionError: broker down" for d in failed)
    assert sent == []
    assert len(result.fix_lanes) == 7
    unbussed = HandlerLabSliAlert().handle(
        ModelLabSliAlertRequest(evaluation=HandlerLabSliEvaluate().handle(_night()))
    )
    assert unbussed.deliveries
    assert not any(d.published for d in unbussed.deliveries)
