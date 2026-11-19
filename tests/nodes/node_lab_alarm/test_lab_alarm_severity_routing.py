# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Severity routing of the lab alarm (OMN-24939): each declared class routes as declared.

The router wraps the real delivery handler (HandlerLabAlarmDeliver) on its in-memory adapters, so the episode rule,
the ledger row and its dedupe window under test are the delivery's own, not a copy.
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.models.lab_alarm_delivery import (
    EnumDeliveryCondition,
    EnumDeliveryStep,
    ModelLabAlarm,
    ModelLabAlarmDeliveryRequest,
)
from omnimarket.nodes.node_lab_alarm.adapters.routed_delivery_adapters import (
    build_routed_delivery,
    require_declared,
)
from omnimarket.nodes.node_lab_alarm.handlers.alarm_route import (
    GatedAlarmChannel,
    HandlerAlarmRoute,
    load_alarm_severity_table,
)
from omnimarket.nodes.node_lab_alarm.handlers.handler_alarm_digest import (
    HandlerAlarmDigest,
)
from omnimarket.nodes.node_lab_alarm.models.model_alarm_severity import (
    EnumAlarmDigestOutcome,
    EnumAlarmSeverity,
    ModelAlarmDigestCommand,
    ModelAlarmSeverityTable,
    UndeclaredAlarmSourceError,
)
from omnimarket.nodes.node_lab_alarm.models.model_lab_alarm_lane_census import (
    EnumLaneCensusCondition,
)
from omnimarket.nodes.node_lab_alarm.overlay import ModelLabAlarmOverlay
from omnimarket.nodes.node_lab_alarm_deliver_effect.adapters.inmemory_adapters import (
    InMemoryAlarmChannel,
    InMemoryAlarmLedger,
    InMemoryDeliveryState,
)
from omnimarket.nodes.node_lab_alarm_deliver_effect.handlers.handler_lab_alarm_deliver import (
    HandlerLabAlarmDeliver,
)
from omnimarket.nodes.node_lab_alarm_deliver_effect.overlay import ModelDeliveryOverlay

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[3]
_NODE = _ROOT / "src/omnimarket/nodes/node_lab_alarm"
_DELIVER = _ROOT / "src/omnimarket/nodes/node_lab_alarm_deliver_effect"
_START = datetime(2026, 11, 16, 9, 0, tzinfo=UTC)


class _Clock:
    def __init__(self, start: datetime = _START) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def tick(self, minutes: int) -> None:
        self.now += timedelta(minutes=minutes)


def _delivery_overlay() -> ModelDeliveryOverlay:
    return ModelDeliveryOverlay.model_validate(
        yaml.safe_load((_DELIVER / "overlay.example.yaml").read_text(encoding="utf-8"))
    )


class _Rig:
    """The router over the real delivery handler, sharing one channel, ledger and digest store with the digest."""

    def __init__(self, table: ModelAlarmSeverityTable | None = None) -> None:
        self.table = table or load_alarm_severity_table()
        self.clock = _Clock()
        self.channel = InMemoryAlarmChannel()
        self.ledger = InMemoryAlarmLedger()
        self.digest_state = InMemoryDeliveryState()
        overlay = _delivery_overlay()
        gate = GatedAlarmChannel(self.channel)
        self.route = HandlerAlarmRoute(
            table=self.table,
            gate=gate,
            channel=self.channel,
            inner=HandlerLabAlarmDeliver(
                overlay=overlay,
                channel=gate,
                ledger=self.ledger,
                state=InMemoryDeliveryState(),
                clock=self.clock,
            ),
            digest=self.digest_state,
            clock=self.clock,
        )
        self.digest = HandlerAlarmDigest(
            table=self.table,
            channel=self.channel,
            ledger=self.ledger,
            digest=self.digest_state,
            lane=overlay.ledger.lane,
            actor=overlay.ledger.actor,
            request_namespace=overlay.ledger.request_namespace,
            clock=self.clock,
        )

    def alarm(self, condition: str, *subjects: str) -> Any:
        return self.route.handle(
            ModelLabAlarmDeliveryRequest(
                correlation_id=f"c-{self.clock.now:%H%M%S}",
                condition=condition,
                outcome=EnumDeliveryCondition.ALARM,
                alarms=tuple(
                    ModelLabAlarm(
                        condition=condition, subject=s, detail=f"{s} is breaching"
                    )
                    for s in subjects
                ),
                evidence="read by the test",
            )
        )

    def ok(self, condition: str) -> Any:
        return self.route.handle(
            ModelLabAlarmDeliveryRequest(
                correlation_id=f"c-{self.clock.now:%H%M%S}-ok",
                condition=condition,
                outcome=EnumDeliveryCondition.OK,
            )
        )

    def friction_rows(self) -> list[str]:
        return [row for _, row in self.ledger.rows if " | FRICTION | " in row]


def _first(severity: EnumAlarmSeverity) -> str:
    table = load_alarm_severity_table()
    return next(s.condition for s in table.of(severity) if s.posts_via == "router")


# --- AC1: declared severity, refused at load ---------------------------------------------------------------------


def _handler_conditions() -> set[str]:
    """Every ``CONDITION = "<name>"`` a node_lab_alarm handler declares, read from the source."""
    found: set[str] = set()
    for path in sorted((_NODE / "handlers").glob("handler_*.py")):
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if (
                isinstance(node, ast.Assign)
                and any(
                    isinstance(t, ast.Name) and t.id == "CONDITION"
                    for t in node.targets
                )
                and isinstance(node.value, ast.Constant)
            ):
                found.add(str(node.value.value))
    return found


def test_alarm_severity_every_node_condition_is_declared() -> None:
    table = load_alarm_severity_table()
    overlay = ModelLabAlarmOverlay.model_validate(
        yaml.safe_load((_NODE / "overlay.example.yaml").read_text(encoding="utf-8"))
    )
    conditions = (
        _handler_conditions()
        | {c.value for c in EnumLaneCensusCondition}
        | {overlay.lag.record_condition}
    )
    assert len(conditions) >= 9
    for condition in sorted(conditions):
        source = table.source(condition)
        assert source.posts_via == "router", condition
        assert source.severity in set(EnumAlarmSeverity)
    require_declared(overlay, table)


def test_alarm_severity_source_without_severity_is_refused_at_load(
    tmp_path: Path,
) -> None:
    contract = yaml.safe_load((_NODE / "contract.yaml").read_text(encoding="utf-8"))
    del contract["alarm_sources"][0]["severity"]
    path = tmp_path / "contract.yaml"
    path.write_text(yaml.safe_dump(contract), encoding="utf-8")
    with pytest.raises(ValueError, match="severity"):
        load_alarm_severity_table(path)


def test_alarm_severity_operation_with_no_source_is_refused_at_load(
    tmp_path: Path,
) -> None:
    contract = yaml.safe_load((_NODE / "contract.yaml").read_text(encoding="utf-8"))
    contract["alarm_sources"] = [
        s for s in contract["alarm_sources"] if s.get("operation") != "lab_pass_receipt"
    ]
    path = tmp_path / "contract.yaml"
    path.write_text(yaml.safe_dump(contract), encoding="utf-8")
    with pytest.raises(ValueError, match="lab_pass_receipt"):
        load_alarm_severity_table(path)


def test_alarm_severity_fix_cause_without_cause_is_refused_at_load(
    tmp_path: Path,
) -> None:
    contract = yaml.safe_load((_NODE / "contract.yaml").read_text(encoding="utf-8"))
    for source in contract["alarm_sources"]:
        if source["severity"] == "fix-cause":
            source["cause"] = ""
            break
    path = tmp_path / "contract.yaml"
    path.write_text(yaml.safe_dump(contract), encoding="utf-8")
    with pytest.raises(ValueError, match="names no cause"):
        load_alarm_severity_table(path)


def test_alarm_severity_undeclared_condition_is_refused_before_any_post() -> None:
    rig = _Rig()
    with pytest.raises(UndeclaredAlarmSourceError):
        rig.alarm("no_such_condition", "x")
    assert rig.channel.posts == []
    assert rig.ledger.rows == []
    overlay_raw = yaml.safe_load(
        (_NODE / "overlay.example.yaml").read_text(encoding="utf-8")
    )
    overlay_raw["lag"]["record_condition"] = "renamed_lag"
    with pytest.raises(UndeclaredAlarmSourceError):
        build_routed_delivery(
            _delivery_overlay(),
            alarm_overlay=ModelLabAlarmOverlay.model_validate(overlay_raw),
        )


# --- AC2: page, flapping -----------------------------------------------------------------------------------------


def test_alarm_page_flapping_one_message_per_condition_and_one_clear() -> None:
    rig = _Rig()
    page = _first(EnumAlarmSeverity.PAGE)
    assert page == "dev_tag_heartbeat"
    first = rig.alarm(page, "omnimarket")
    assert [d.channel for d in first.deliveries] == [EnumDeliveryStep.DELIVERED]
    assert len(rig.channel.posts) == 1
    # The condition holds: nothing more is sent.
    for _ in range(5):
        rig.clock.tick(10)
        held = rig.alarm(page, "omnimarket")
        assert [d.channel for d in held.deliveries] == [EnumDeliveryStep.SUPPRESSED]
    assert len(rig.channel.posts) == 1
    # It clears: one clear message.
    rig.clock.tick(10)
    cleared = rig.ok(page)
    assert cleared.cleared == (f"{page}:omnimarket",)
    assert len(rig.channel.posts) == 2
    assert "cleared" in rig.channel.posts[1][1]
    assert "omnimarket" in rig.channel.posts[1][1]
    # A second OK clears nothing more and posts nothing.
    rig.clock.tick(10)
    rig.ok(page)
    assert len(rig.channel.posts) == 2
    # It raises again: one new message for the new condition.
    rig.clock.tick(10)
    again = rig.alarm(page, "omnimarket")
    assert [d.channel for d in again.deliveries] == [EnumDeliveryStep.DELIVERED]
    rig.clock.tick(10)
    rig.alarm(page, "omnimarket")
    assert len(rig.channel.posts) == 3
    texts = [text for _, text in rig.channel.posts]
    assert sum("cleared" in t for t in texts) == 1
    # The ledger's own dedupe window keeps one row for the flapping subject.
    assert len(rig.friction_rows()) == 1


# --- AC3: digest -------------------------------------------------------------------------------------------------


def test_alarm_digest_holds_every_occurrence_and_sends_one_message_a_day() -> None:
    rig = _Rig()
    digest = _first(EnumAlarmSeverity.DIGEST)
    for hour in range(6):
        rig.alarm(digest, "dev-broker-a", "dev-broker-b")
        rig.clock.tick(30)
        rig.ok(digest)
        rig.clock.tick(30)
        assert hour >= 0
    rig.alarm("stale_indeterminate", "consumer_group_lag")
    # No occurrence was posted; the ledger row was still written.
    assert rig.channel.posts == []
    assert rig.friction_rows()
    # Same day: nothing is due yet.
    same_day = rig.digest.handle(ModelAlarmDigestCommand(correlation_id="d0"))
    assert same_day.outcome is EnumAlarmDigestOutcome.NOTHING_DUE
    assert rig.channel.posts == []
    # Next day: exactly one message for the day's DIGEST events.
    rig.clock.now = datetime(2026, 11, 17, 0, 5, tzinfo=UTC)
    sent = rig.digest.handle(ModelAlarmDigestCommand(correlation_id="d1"))
    assert sent.outcome is EnumAlarmDigestOutcome.SENT
    assert len(rig.channel.posts) == 1
    category, text = rig.channel.posts[0]
    assert category == rig.table.routing.digest_category
    assert "2026-11-16" in text
    assert "13 new conditions from 2 sources" in text
    assert digest in text
    assert "stale_indeterminate" in text
    assert [(d.day, d.episodes, d.sources) for d in sent.days] == [
        ("2026-11-16", 13, 2)
    ]
    # A rerun sends nothing more for that day.
    rig.clock.tick(60)
    again = rig.digest.handle(ModelAlarmDigestCommand(correlation_id="d2"))
    assert again.outcome is EnumAlarmDigestOutcome.NOTHING_DUE
    assert len(rig.channel.posts) == 1


def test_alarm_digest_failed_post_keeps_the_day_for_the_next_run() -> None:
    rig = _Rig()
    rig.alarm(_first(EnumAlarmSeverity.DIGEST), "dev-broker-a")
    rig.clock.now = datetime(2026, 11, 17, 0, 5, tzinfo=UTC)
    rig.channel.refuse = "channel down"
    failed = rig.digest.handle(ModelAlarmDigestCommand(correlation_id="d1"))
    assert failed.outcome is EnumAlarmDigestOutcome.FAILED
    rig.channel.refuse = None
    sent = rig.digest.handle(ModelAlarmDigestCommand(correlation_id="d2"))
    assert sent.outcome is EnumAlarmDigestOutcome.SENT
    assert len(rig.channel.posts) == 1


def test_alarm_digest_probe_sends_nothing_and_keeps_nothing() -> None:
    rig = _Rig()
    rig.alarm(_first(EnumAlarmSeverity.DIGEST), "dev-broker-a")
    rig.clock.now = datetime(2026, 11, 17, 0, 5, tzinfo=UTC)
    rows = len(rig.ledger.rows)
    probe = rig.digest.handle(ModelAlarmDigestCommand(correlation_id="p", write=False))
    assert probe.outcome is EnumAlarmDigestOutcome.SKIPPED
    assert [d.day for d in probe.days] == ["2026-11-16"]
    assert rig.channel.posts == []
    assert len(rig.ledger.rows) == rows


# --- AC4: drop ---------------------------------------------------------------------------------------------------


def test_alarm_drop_posts_nothing_and_still_writes_its_ledger_row() -> None:
    rig = _Rig()
    drop = _first(EnumAlarmSeverity.DROP)
    assert drop == "consumer_group_lag"
    result = rig.alarm(drop, "group-a")
    assert [d.channel for d in result.deliveries] == [EnumDeliveryStep.SUPPRESSED]
    assert [d.channel_ref for d in result.deliveries] == ["held:drop"]
    assert [d.ledger for d in result.deliveries] == [EnumDeliveryStep.DELIVERED]
    rig.clock.tick(10)
    rig.ok(drop)
    assert rig.channel.posts == []
    rows = rig.friction_rows()
    assert len(rows) == 1
    assert "condition=consumer_group_lag" in rows[0]
    # A dropped source is never in the digest either.
    rig.clock.now = datetime(2026, 11, 17, 0, 5, tzinfo=UTC)
    rig.digest.handle(ModelAlarmDigestCommand(correlation_id="d1"))
    assert rig.channel.posts == []


# --- AC5: fix-cause ----------------------------------------------------------------------------------------------


def test_alarm_fix_cause_posts_as_page_and_is_listed_in_one_status_row() -> None:
    rig = _Rig()
    fix = _first(EnumAlarmSeverity.FIX_CAUSE)
    first = rig.alarm(fix, "delegate-skill")
    assert [d.channel for d in first.deliveries] == [EnumDeliveryStep.DELIVERED]
    rig.clock.tick(10)
    rig.alarm(fix, "delegate-skill")
    rig.clock.tick(10)
    rig.ok(fix)
    assert len(rig.channel.posts) == 2
    # The digest step writes one STATUS row naming every fix-cause source and its cause.
    result = rig.digest.handle(ModelAlarmDigestCommand(correlation_id="d1"))
    assert result.status_row == "accepted"
    rig.clock.tick(60)
    rerun = rig.digest.handle(ModelAlarmDigestCommand(correlation_id="d2"))
    assert rerun.status_row == "duplicate"
    status = [row for _, row in rig.ledger.rows if " | STATUS | " in row]
    assert len(status) == 1
    row = status[0]
    fix_sources = rig.table.of(EnumAlarmSeverity.FIX_CAUSE)
    assert f"sources={len(fix_sources)}" in row
    for source in fix_sources:
        assert source.condition in row
        assert source.cause.replace("|", "/") in row
    assert "9,699 queued" in row
    assert "routing_tiers.yaml" in row
    assert len(row.split(" | ")) == 10


def test_alarm_fix_cause_and_page_share_one_route() -> None:
    table = load_alarm_severity_table()
    assert table.of(EnumAlarmSeverity.FIX_CAUSE)
    assert all(s.cause for s in table.of(EnumAlarmSeverity.FIX_CAUSE))
