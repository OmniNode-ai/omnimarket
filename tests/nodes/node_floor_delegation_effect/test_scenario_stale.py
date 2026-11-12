# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A scenario read older than its limit is stale since its read time, never 'unread' as if it were current (OMN-23969)."""

from __future__ import annotations

from datetime import timedelta

from omnimarket.nodes.node_floor_delegation_effect.handlers.adapter_delegation_tracker import (
    build_tracker,
)
from omnimarket.nodes.node_floor_delegation_effect.models.model_floor_delegation import (
    ModelDelegationTracker,
    ModelScenarioRead,
    ModelScenarioResult,
)

from .support import NOW, RT, all_reads, inputs, iso, read, run

OLD_MINUTES = RT.scenario_reads_max_age_seconds // 60 + 5


def _build(reads: tuple[ModelScenarioRead, ...]) -> ModelDelegationTracker:
    return build_tracker(RT, inputs(reads=reads), NOW)


def _scenario(doc: ModelDelegationTracker, scenario_id: str) -> ModelScenarioResult:
    return next(s for s in doc.scenarios if s.id == scenario_id)


def test_a_read_older_than_its_limit_is_stale_since_its_read_time() -> None:
    reads = list(all_reads("success"))
    reads[4] = read("R5", run("success", 10), read_minutes_ago=OLD_MINUTES)
    doc = _build(tuple(reads))
    r5 = _scenario(doc, "R5")
    since = iso(NOW - timedelta(minutes=OLD_MINUTES))
    assert r5.state == "stale"
    assert r5.stale_since == since
    assert r5.result_text == f"Stale since {since}"
    assert "last result" in r5.reason.lower()
    assert r5.latest_conclusion == "success"


def test_a_stale_scenario_is_counted_stale_not_unread_and_never_a_pass() -> None:
    reads = list(all_reads("success"))
    reads[4] = read("R5", run("success", 10), read_minutes_ago=OLD_MINUTES)
    doc = _build(tuple(reads))
    assert doc.counts.scenarios_stale == 1
    assert doc.counts.scenarios_unread == 0
    assert doc.counts.scenarios_passing == 8
    assert doc.blocking == ("R5",)
    assert doc.shippable is False


def test_control_the_same_read_inside_its_limit_is_a_pass() -> None:
    """The failing control: a read one minute old stays a pass, so only age makes it stale."""
    doc = _build(all_reads("success"))
    r5 = _scenario(doc, "R5")
    assert r5.state == "pass"
    assert r5.stale_since == ""
    assert doc.counts.scenarios_stale == 0


def test_a_stale_failed_read_is_stale_with_no_last_result() -> None:
    reads = list(all_reads("success"))
    reads[3] = read("R4", kind="unread", reason="exit 69", read_minutes_ago=OLD_MINUTES)
    doc = _build(tuple(reads))
    r4 = _scenario(doc, "R4")
    assert r4.state == "stale"
    assert r4.stale_since == iso(NOW - timedelta(minutes=OLD_MINUTES))
    assert r4.latest_conclusion == ""
    assert "exit 69" in r4.reason


def test_a_scenario_never_read_stays_unread() -> None:
    doc = _build(all_reads("success")[:-1])
    r9 = _scenario(doc, "R9")
    assert r9.state == "unread"
    assert r9.stale_since == ""
    assert doc.counts.scenarios_stale == 0
    assert doc.counts.scenarios_unread == 1
