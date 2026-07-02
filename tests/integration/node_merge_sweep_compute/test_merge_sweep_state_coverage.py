# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract-declared state-coverage regression tests for node_merge_sweep_compute.

OMN-13674 (cluster merge_sweep_pr_lifecycle_compute) under the full
declared-state-coverage DoD and the AST-hardened state-coverage gate
(OMN-13816 / OMN-13781). Pins this COMPUTE node's contract-declared terminal
event / publish topic — the state the runtime auto-emits — plus its classified
track enum and failure-category enum to their literal declared values. A silent
contract rename or a dropped track/category now fails here instead of only
surfacing at a live runtime/projection boundary.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from omnimarket.nodes.node_merge_sweep_compute.handlers.handler_merge_sweep import (
    EnumFailureCategory,
    EnumPRTrack,
)

_CONTRACT_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_merge_sweep_compute"
    / "contract.yaml"
)


def _load_contract() -> dict[str, Any]:
    return yaml.safe_load(_CONTRACT_PATH.read_text())


def test_merge_sweep_declares_terminal_and_publish_topic() -> None:
    """The terminal event and publish topic keep their literal wire string."""
    contract = _load_contract()
    assert contract["terminal_event"] == "onex.evt.omnimarket.merge-sweep-completed.v1"
    assert (
        "onex.evt.omnimarket.merge-sweep-completed.v1"
        in contract["event_bus"]["publish_topics"]
    )


def test_merge_sweep_declares_command_and_dlq_topics() -> None:
    """The subscribe command topic and DLQ topic keep their literal wire strings."""
    event_bus = _load_contract()["event_bus"]
    assert "onex.cmd.omnimarket.merge-sweep-start.v1" in event_bus["subscribe_topics"]
    assert "onex.dlq.omnimarket.merge-sweep.v1" in event_bus["dlq_topics"]


def test_merge_sweep_track_classes_are_pinned() -> None:
    """Every declared classification track keeps its literal wire value."""
    values = {member.value for member in EnumPRTrack}
    assert values == {"A-update", "A", "A-resolve", "B", "skip"}


def test_merge_sweep_failure_categories_are_pinned() -> None:
    """Every declared failure category keeps its literal wire value."""
    values = {member.value for member in EnumFailureCategory}
    assert values == {
        "ci_test",
        "ci_lint",
        "ci_gate",
        "pr_title",
        "conflict",
        "changes_requested",
        "threads_blocked",
        "branch_stale",
        "scan_failed",
        "polish_failed",
        "needs_human",
    }
