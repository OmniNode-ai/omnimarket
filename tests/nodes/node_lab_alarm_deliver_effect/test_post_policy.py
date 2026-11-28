# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The alert channel's post policy against the burst recorded in #omninode-notifications (OMN-24562).

Each recorded burst yields one post; each self-healing or unreadable message yields none.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from omnimarket.handlers.alarm_post_policy import AlarmPostGate
from omnimarket.models.alarm_post_policy import ModelAlarmPostPolicy
from omnimarket.models.alarm_send_receipt import ModelAlarmSendReceipt
from omnimarket.nodes.node_lab_alarm_deliver_effect.adapters.inmemory_adapters import (
    InMemoryAlarmChannel,
    InMemoryDeliveryState,
)
from omnimarket.nodes.node_lab_alarm_deliver_effect.adapters.policy_gated_channel import (
    PolicyGatedChannel,
)
from omnimarket.nodes.node_lab_alarm_deliver_effect.overlay import ModelDeliveryOverlay

pytestmark = pytest.mark.unit

_NODE = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_lab_alarm_deliver_effect"
)
_T0 = datetime(2026, 11, 27, 21, 12, tzinfo=UTC)
_JOB = (
    "JOB EXIT STILL OPEN ai.omninode.work-ledger-serve exited nonzero {runs} runs in a row, "
    "episode open {minutes} minutes, raised {raised} times, no CLAIM cites it"
)


class _Clock:
    def __init__(self) -> None:
        self.now = _T0

    def __call__(self) -> datetime:
        return self.now


class _Rig:
    def __init__(self, ticket_after_hours: int | None = None) -> None:
        doc = yaml.safe_load(
            (_NODE / "overlay.example.yaml").read_text(encoding="utf-8")
        )
        if ticket_after_hours is not None:
            doc["post_policy"]["ticket_after_hours"] = ticket_after_hours
        overlay = ModelDeliveryOverlay.model_validate(doc)
        assert overlay.post_policy is not None
        self.clock = _Clock()
        self.state = InMemoryDeliveryState()
        self.inner = InMemoryAlarmChannel()
        self.readings: list[tuple[str, str]] = []
        self.tickets: list[str] = []
        self.channel = PolicyGatedChannel(
            inner=self.inner,
            policy=lambda: overlay.post_policy,
            state=self.state,
            clock=self.clock,
            reading=lambda d, _t: self.readings.append((d.kind, d.reason)),
            ticket=lambda _p, d: self.tickets.append(d.key) is None,
        )

    def send(self, text: str) -> ModelAlarmSendReceipt:
        return self.channel.send(category="lab-alarm", text=text)

    @property
    def posts(self) -> list[str]:
        return [text for _, text in self.inner.posts]


def test_quiet_host_from_four_hosts_posts_once() -> None:
    rig = _Rig()
    for host in ("h051", "h052", "h053", "h200"):
        rig.send(
            f"QUIET HOST: lab-wide hook capture silent (seen by {host}, 7 hosts quiet)"
        )
        rig.clock.now += timedelta(minutes=3)
    assert len(rig.posts) == 1
    assert rig.readings == [("quiet_host", "duplicate")] * 3


def test_quiet_host_open_a_day_is_ticketed_instead_of_reposted() -> None:
    rig = _Rig()
    rig.send("QUIET HOST: capture silent")
    rig.clock.now += timedelta(hours=25)
    rig.send("QUIET HOST: capture silent")
    assert len(rig.posts) == 1
    assert rig.tickets == ["quiet_host"]


def test_quiet_host_reposts_after_a_day_not_before() -> None:
    rig = _Rig(ticket_after_hours=72)
    rig.send("QUIET HOST: capture silent")
    rig.clock.now += timedelta(hours=23)
    rig.send("QUIET HOST: capture silent")
    assert len(rig.posts) == 1
    rig.clock.now += timedelta(hours=2)
    rig.send("QUIET HOST: capture silent")
    assert len(rig.posts) == 2


def test_self_healing_host_drift_never_posts() -> None:
    rig = _Rig()
    for text in (
        "[host-drift] DETECTED h052 plugin behind, syncing now",
        "[host-drift] DETECTED h053 plugin behind, self-healing via launchd",
    ):
        rig.send(text)
    assert rig.posts == []
    assert rig.readings == [("host_drift", "self-healing")] * 2


def test_unit_drift_unread_does_not_post_until_two_hours_then_once() -> None:
    rig = _Rig()
    text = "[host-drift] DETECTED h051 unit-drift unread: timed out"
    rig.send(text)
    rig.clock.now += timedelta(hours=1)
    rig.send(text)
    assert rig.posts == []
    rig.clock.now += timedelta(hours=1, minutes=1)
    rig.send(text)
    rig.send(text)
    assert len(rig.posts) == 1
    assert "detector host_drift" in rig.posts[0]


def test_real_host_drift_posts_once_per_condition_and_resolves_once() -> None:
    rig = _Rig()
    drift = "[host-drift] DETECTED h052 clone drift: stray file in the infra clone"
    rig.send(drift)
    rig.send(drift)
    assert len(rig.posts) == 1
    gate = AlarmPostGate(
        ModelAlarmPostPolicy.model_validate(
            yaml.safe_load(
                (_NODE / "overlay.example.yaml").read_text(encoding="utf-8")
            )["post_policy"]
        )
    )
    held = rig.state.load()
    subject = "h052 clone drift: stray file in the infra clone"
    cleared = gate.resolve("host_drift", subject, now=rig.clock.now, state=held)
    assert cleared.action.value == "resolved"
    again = gate.resolve("host_drift", subject, now=rig.clock.now, state=held)
    assert again.action.value == "suppress"


def test_job_exit_open_for_days_is_ticketed_not_posted() -> None:
    rig = _Rig()
    for runs, raised in ((28, 59), (29, 60), (30, 61)):
        rig.send(_JOB.format(runs=runs, minutes=40286, raised=raised))
    assert rig.posts == []
    assert rig.tickets == ["job_exit_still_open:ai.omninode.work-ledger-serve"]
    assert [r for _, r in rig.readings] == ["stale-no-owner", "ticketed", "ticketed"]


def test_job_exit_fresh_episode_posts_once_with_a_counter_change_ignored() -> None:
    rig = _Rig()
    rig.send(_JOB.format(runs=3, minutes=45, raised=1))
    rig.clock.now += timedelta(minutes=30)
    rig.send(_JOB.format(runs=4, minutes=75, raised=2))
    assert len(rig.posts) == 1


def test_detector_unreadable_never_posts_as_a_condition() -> None:
    rig = _Rig()
    for _ in range(3):
        rig.send("Merge override detector unreadable for OmniNode-ai/omnimarket#6900")
        rig.clock.now += timedelta(minutes=20)
    assert rig.posts == []


def test_merge_without_override_is_a_reading() -> None:
    rig = _Rig()
    rig.send(
        "Merge without override: OmniNode-ai/omnimarket#6901 head abc landed by the controller"
    )
    assert rig.posts == []
    assert rig.readings == [("merge_without_override", "quiet")]


def test_unmatched_failure_still_posts_once() -> None:
    rig = _Rig()
    for _ in range(3):
        rig.send("FIX-FAILED after 3 attempts: runner-fleet canary")
    assert len(rig.posts) == 1


def test_page_kind_without_an_action_is_refused() -> None:
    doc = yaml.safe_load((_NODE / "overlay.example.yaml").read_text(encoding="utf-8"))
    doc["post_policy"]["kinds"][0]["action"] = ""
    with pytest.raises(ValueError, match="names no action"):
        ModelAlarmPostPolicy.model_validate(doc["post_policy"])
