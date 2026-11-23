# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_lab_alarm declares the topic the runtime consumes, and the tick still runs the alarm once (OMN-26509).

Before this change the contract declared ``event_bus.subscribe_topics: []`` beside an external command topic, so the runtime's
auto-wiring skipped it ("wiring outcome is skipped, not WIRED") and its log held wiring lines only. The contract is read here,
and wired through infra's own auto-wiring.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.runtime.auto_wiring.models import ModelAutoWiringManifest
from omnibase_infra.runtime.auto_wiring.report import EnumWiringOutcome

from tests.unit.contracts.test_every_node_auto_wires_in_process_omn17427 import (
    _wire_and_collect_failures,
)

pytestmark = pytest.mark.unit

NODES = Path(__file__).resolve().parents[3] / "src" / "omnimarket" / "nodes"
ALARM = NODES / "node_lab_alarm" / "contract.yaml"
SCHEDULED = NODES / "node_lab_alarm_scheduled_effect" / "contract.yaml"
TICK_TOPIC = "onex.intent.platform.runtime-tick.v1"


def _load(path: Path) -> dict[str, Any]:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_the_alarm_subscribes_to_its_command_topic() -> None:
    contract = _load(ALARM)
    command = contract["runtime_dispatch"]["command_topic"]
    assert command == "onex.cmd.omnimarket.lab-alarm-broker-lag-requested.v1"
    assert contract["event_bus"]["subscribe_topics"] == [command]


def test_the_alarm_does_not_subscribe_to_the_tick_the_scheduled_node_runs_it_on() -> (
    None
):
    """A second subscriber on the tick would run broker-lag twice per tick."""
    alarm = _load(ALARM)["event_bus"]["subscribe_topics"]
    assert TICK_TOPIC not in alarm
    assert _load(SCHEDULED)["event_bus"]["subscribe_topics"] == [TICK_TOPIC]


async def test_the_alarm_wires_and_is_not_skipped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path / "no-overlays"))
    discovered = discover_contracts_from_paths([ALARM])
    assert not discovered.errors, discovered.errors
    report, failing = await _wire_and_collect_failures(
        monkeypatch, ModelAutoWiringManifest(contracts=tuple(discovered.contracts))
    )
    outcomes = {r.contract_name: r.outcome for r in report.results}
    assert not failing, failing
    assert outcomes == {"node_lab_alarm": EnumWiringOutcome.WIRED}
