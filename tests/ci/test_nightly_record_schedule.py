# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The attribution record's schedule is the existing nightly poll, not a new script, workflow or timer. Focused name: schedule."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
NODE_DIR = ROOT / "src/omnimarket/nodes/node_test_selector_miss_collect_effect"
WORKFLOWS = ROOT / ".github/workflows"
NIGHTLY = WORKFLOWS / "nightly-full-suite.yml"
OPERATION = "publish_attribution_record"
SCRIPT_SUFFIXES = {".sh", ".bash", ".zsh", ".ps1"}


def _contract() -> dict[str, Any]:
    loaded = yaml.safe_load((NODE_DIR / "contract.yaml").read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _schedule() -> dict[str, Any]:
    schedule = _contract().get("schedule")
    assert isinstance(schedule, dict), "the node contract declares no schedule block"
    return schedule


def test_schedule_is_declared_in_the_node_contract_on_the_existing_workflow() -> None:
    schedule = _schedule()
    assert schedule["trigger"] == "nightly_workflow_run_poll"
    assert schedule["workflow_file"] == "nightly-full-suite.yml"
    assert NIGHTLY.is_file()
    assert schedule["new_script"] is False


def test_schedule_runs_the_publish_after_the_collect_of_the_same_poll() -> None:
    schedule = _schedule()
    assert schedule["operations_in_order"] == ["collect_nightly", OPERATION]
    declared = {e["operation"] for e in _contract()["cli_handlers"]}
    assert set(schedule["operations_in_order"]) <= declared


def test_schedule_command_topic_is_declared_with_the_nightly_poller_as_producer() -> (
    None
):
    contract = _contract()
    topic = _schedule()["command_topic"]
    produced = {
        e["topic"]: e["producer"] for e in contract["externally_produced_topics"]
    }
    assert topic in produced
    assert "poller of the nightly workflow's runs" in produced[topic]
    subscribed = {s["topic"]: s["operation"] for s in contract["input_subscriptions"]}
    assert subscribed[topic] == OPERATION


def test_schedule_the_nightly_workflow_keeps_its_own_trigger_and_names_no_publish() -> (
    None
):
    text = NIGHTLY.read_text(encoding="utf-8")
    workflow = yaml.safe_load(text)
    triggers = workflow.get("on", workflow.get(True))
    assert set(triggers) == {"schedule", "workflow_dispatch"}
    assert triggers["schedule"]
    assert OPERATION not in text
    assert "attribution" not in text.lower()


def test_schedule_adds_no_script_workflow_or_timer() -> None:
    node_files = [p for p in NODE_DIR.rglob("*") if p.is_file()]
    assert not [p for p in node_files if p.suffix in SCRIPT_SUFFIXES]
    assert not [p for p in node_files if p.name.endswith((".service", ".timer"))]
    assert not list((ROOT / "scripts").rglob("*attribution*record*"))
    assert not list((ROOT / "scripts").rglob("*publish*attribution*"))
    referencing = [
        w.name
        for w in WORKFLOWS.glob("*.y*ml")
        if re.search(OPERATION, w.read_text(encoding="utf-8"))
    ]
    assert referencing == []
    # the node declares no timer of its own: the poll that already exists is the schedule
    assert not {"unit_name", "calendar", "on_calendar", "cron"} & set(_schedule())
