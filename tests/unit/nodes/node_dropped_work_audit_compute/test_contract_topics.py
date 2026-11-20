# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The topics node_dropped_work_audit_compute declares (OMN-26205)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

CONTRACT = (
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/nodes/node_dropped_work_audit_compute/contract.yaml"
)


def test_terminal_event_is_the_declared_output_and_is_published() -> None:
    loaded = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    assert (
        loaded["terminal_event"]
        == "onex.evt.omnimarket.dropped-work-audit-completed.v1"
    )
    assert (
        "onex.evt.omnimarket.dropped-work-audit-completed.v1"
        in loaded["event_bus"]["publish_topics"]
    )


def test_command_topic_is_the_subscription_and_the_operation_route() -> None:
    loaded = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    assert loaded["runtime_dispatch"]["command_topic"] == (
        "onex.cmd.omnimarket.dropped-work-audit-requested.v1"
    )
    assert loaded["event_bus"]["subscribe_topics"] == [
        "onex.cmd.omnimarket.dropped-work-audit-requested.v1"
    ]
