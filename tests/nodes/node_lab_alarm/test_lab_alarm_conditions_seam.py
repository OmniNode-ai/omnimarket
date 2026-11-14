# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The seam of the four census, receipt and staleness conditions of node_lab_alarm (OMN-24425).

Condition -> alarm row -> alert publisher: each condition's ALARM goes through the real
HandlerLabAlarmDeliver on in-memory adapters, and the row it appends and the post it makes carry the condition,
the subject and the reading. Condition -> contract: every condition's four topics are declared in the contract and
the three it emits are externally consumed. Condition -> schedule: every one has its own schedule in the overlay
and a verb. A change to the shape of the condition's alarm line, the delivery request, the row or the contract
turns one of these red.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_lab_alarm.adapters.condition_adapters import (
    InMemoryCensusSource,
    InMemoryDeliveredShaSource,
    InMemoryLabPassReceiptSource,
    InMemoryRunRecordReader,
)
from omnimarket.nodes.node_lab_alarm.handlers.handler_lab_pass_receipt import (
    HandlerLabPassReceipt,
)
from omnimarket.nodes.node_lab_alarm.handlers.handler_lane_census import (
    HandlerLaneCensus,
)
from omnimarket.nodes.node_lab_alarm.handlers.handler_stale_indeterminate import (
    HandlerStaleIndeterminate,
)
from omnimarket.nodes.node_lab_alarm.handlers.lane_census_snapshot import (
    FINDING_KIND_SEVERITY,
    alert_key,
)
from omnimarket.nodes.node_lab_alarm.models.model_lab_alarm_lab_pass_receipt import (
    ModelLabPassReceiptCommand,
)
from omnimarket.nodes.node_lab_alarm.models.model_lab_alarm_lane_census import (
    EnumLaneCensusCondition,
    ModelLaneCensusCommand,
)
from omnimarket.nodes.node_lab_alarm.models.model_lab_alarm_stale_indeterminate import (
    ModelStaleIndeterminateCommand,
)
from omnimarket.nodes.node_lab_alarm.overlay import (
    SCHEDULABLE_VERBS,
    ModelLabAlarmOverlay,
)
from omnimarket.nodes.node_lab_alarm.runtime_lab_alarm_conditions import (
    CONDITION_VERBS,
)
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

_NODE = Path(__file__).resolve().parents[3] / "src/omnimarket/nodes/node_lab_alarm"
_DELIVER = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_lab_alarm_deliver_effect"
)
_CLOCK = datetime(2026, 11, 14, 4, 0, tzinfo=UTC)
_SHA = "a" * 40
#: condition name -> the verb its unit runs
_VERBS = {
    "lane_drift": "lane-drift",
    "container_unhealthy": "container-unhealthy",
    "lab_pass_receipt": "lab-pass-receipt",
    "stale_indeterminate": "stale-indeterminate",
}


def _overlay_raw() -> dict[str, Any]:
    raw = yaml.safe_load((_NODE / "overlay.example.yaml").read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return raw


def _contract() -> dict[str, Any]:
    contract = yaml.safe_load((_NODE / "contract.yaml").read_text(encoding="utf-8"))
    assert isinstance(contract, dict)
    return contract


class _Publisher:
    def __init__(self) -> None:
        self.channel = InMemoryAlarmChannel()
        self.ledger = InMemoryAlarmLedger()
        self.delivery = HandlerLabAlarmDeliver(
            overlay=ModelDeliveryOverlay.model_validate(
                yaml.safe_load(
                    (_DELIVER / "overlay.example.yaml").read_text(encoding="utf-8")
                )
            ),
            channel=self.channel,
            ledger=self.ledger,
            state=InMemoryDeliveryState(),
            clock=lambda: _CLOCK,
        )


def _census_snapshot() -> dict[str, Any]:
    findings = [
        {
            "lane": "dev",
            "kind": "container_unhealthy",
            "container": "runtime",
            "detail": "unhealthy for 2000s",
            "severity": FINDING_KIND_SEVERITY["container_unhealthy"],
        },
        {
            "lane": "dev",
            "kind": "container_absent",
            "container": "broker",
            "detail": "no such container",
            "severity": FINDING_KIND_SEVERITY["container_absent"],
        },
    ]
    snapshot: dict[str, Any] = {
        "schema_version": "1.1.0",
        "event_type": "lane-census-drift",
        "topic": "onex.evt.infra.lane-census-drift.v1",
        "host": "host-1",
        "host_id": None,
        "emitted_at": _CLOCK.isoformat(),
        "severity": "critical",
        "lanes_checked": ["dev"],
        "lanes_not_applicable": [],
        "lanes_skipped_optional_down": [],
        "drift_count": len(findings),
        "findings": findings,
        "alert_key": "",
        "ticket_title": "t",
        "ticket_body": "b",
    }
    snapshot["alert_key"] = alert_key("host-1", snapshot)
    return snapshot


def _alarmed(condition: str, publisher: _Publisher) -> tuple[Any, str]:
    """Run the condition's handler to ALARM against the real delivery; returns the result and the subject delivered."""
    overlay = ModelLabAlarmOverlay.model_validate(_overlay_raw())
    if condition in ("lane_drift", "container_unhealthy"):
        assert overlay.lane_census is not None
        snapshot = _census_snapshot()
        census = HandlerLaneCensus(
            condition=EnumLaneCensusCondition(condition),
            settings=overlay.lane_census,
            source=InMemoryCensusSource(document=snapshot),
            delivery=publisher.delivery,
            clock=lambda: _CLOCK,
        )
        return census.handle(ModelLaneCensusCommand(correlation_id="seam-1")), str(
            snapshot["alert_key"]
        )
    if condition == "lab_pass_receipt":
        assert overlay.lab_pass_receipt is not None
        receipt = {
            "sha": _SHA,
            "lane": overlay.lab_pass_receipt.lane,
            "started_at": "2026-11-14T03:50:00+00:00",
            "finished_at": "2026-11-14T03:55:00+00:00",
            "result": "FAIL",
            "checks": [{"name": "boot", "ok": False, "evidence": "refused"}],
        }
        receipts = HandlerLabPassReceipt(
            settings=overlay.lab_pass_receipt,
            sha_source=InMemoryDeliveredShaSource(sha=_SHA),
            receipts=InMemoryLabPassReceiptSource(documents={_SHA: receipt}),
            delivery=publisher.delivery,
            clock=lambda: _CLOCK,
        )
        return receipts.handle(
            ModelLabPassReceiptCommand(correlation_id="seam-1")
        ), f"lab-pass-receipt:{_SHA}"
    assert overlay.stale_indeterminate is not None
    watched = overlay.stale_indeterminate.watched[0].condition
    stale = HandlerStaleIndeterminate(
        settings=overlay.stale_indeterminate,
        reader=InMemoryRunRecordReader(
            outcomes={
                watched: ["INDETERMINATE"] * overlay.stale_indeterminate.after_runs
            }
        ),
        delivery=publisher.delivery,
        clock=lambda: _CLOCK,
    )
    return stale.handle(
        ModelStaleIndeterminateCommand(correlation_id="seam-1")
    ), f"stale-indeterminate:{watched}"


@pytest.mark.parametrize("condition", sorted(_VERBS))
def test_conditions_seam_an_alarm_reaches_the_row_and_the_post_through_the_real_delivery(
    condition: str,
) -> None:
    publisher = _Publisher()
    result, subject = _alarmed(condition, publisher)
    assert result.outcome.value == "ALARM"
    assert result.delivery == (
        f"{condition}:{subject} channel=delivered ledger=delivered",
    )
    [(_, text)] = publisher.channel.posts
    assert condition in text
    assert subject in text
    [(_, row)] = publisher.ledger.rows
    cells = row.split(" | ")
    assert cells[1] == "FRICTION"
    assert f"subject={subject}" in cells
    assert any(f"raised {condition} on" in cell for cell in cells)


@pytest.mark.parametrize("condition", sorted(_VERBS))
def test_conditions_seam_the_contract_declares_the_condition_topics_and_exposes_its_events(
    condition: str,
) -> None:
    contract = _contract()
    topics = contract["topics"]
    kebab = _VERBS[condition]
    for role, kind, suffix in (
        ("requested", "cmd", "requested"),
        ("run_started", "evt", "started"),
        ("run_completed", "evt", "completed"),
        ("run_failed", "evt", "failed"),
    ):
        key = f"{condition}_{role}"
        assert topics[key] == f"onex.{kind}.omnimarket.lab-alarm-{kebab}-{suffix}.v1"
    for role in ("run_started", "run_completed", "run_failed"):
        assert topics[f"{condition}_{role}"] in contract["externally_consumed_topics"]
    assert condition in {entry["operation"] for entry in contract["cli_handlers"]}


@pytest.mark.parametrize("condition", sorted(_VERBS))
def test_conditions_seam_every_condition_has_its_own_verb_probe_and_schedule(
    condition: str,
) -> None:
    verb = _VERBS[condition]
    assert verb in SCHEDULABLE_VERBS
    assert verb in CONDITION_VERBS
    assert f"{verb}-probe" in CONDITION_VERBS
    schedules = ModelLabAlarmOverlay.model_validate(_overlay_raw()).condition_schedules
    assert schedules is not None
    assert verb in schedules
    units = [s.unit_name for s in schedules.values()]
    assert len(units) == len(set(units)), "no two conditions share a unit"


def test_conditions_seam_removing_a_conditions_schedule_leaves_the_others_loadable() -> (
    None
):
    raw = _overlay_raw()
    del raw["condition_schedules"]["lane-drift"]
    schedules = ModelLabAlarmOverlay.model_validate(raw).condition_schedules
    assert schedules is not None
    assert "lane-drift" not in schedules
    assert "container-unhealthy" in schedules
