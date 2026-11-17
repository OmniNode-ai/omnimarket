# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The terminal event node_stall_responder_scheduled_effect declares is a topic it publishes (OMN-25160)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_stall_responder_scheduled_effect/contract.yaml"
)


def test_terminal_event_is_the_declared_output_and_is_published() -> None:
    loaded = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    assert loaded["terminal_event"] == "onex.evt.omnimarket.schedule-run.v1"
    assert (
        "onex.evt.omnimarket.schedule-run.v1" in loaded["event_bus"]["publish_topics"]
    )


def test_subscription_topic_and_operation_are_as_declared() -> None:
    loaded = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    subs = loaded["input_subscriptions"]
    assert len(subs) == 1
    assert subs[0]["topic"] == "onex.intent.platform.runtime-tick.v1"
    assert subs[0]["operation"] == "stall_responder.scheduled_run"


OCC_CONTRACT = Path(__file__).resolve().parents[3] / "contracts/OMN-25160.yaml"


def test_ac4_is_bound_to_a_host_read_that_a_pytest_run_cannot_satisfy() -> None:
    """AC4 reads the h201 schedule-run event for job stall_responder under the local Done gate, with its prerequisites named."""
    loaded = yaml.safe_load(OCC_CONTRACT.read_text(encoding="utf-8"))
    bound = [e for e in loaded["dod_evidence"] if "AC4" in e.get("binds_ac", [])]
    assert len(bound) == 1
    entry = bound[0]
    assert entry["execution_scope"] == "local_done_gate"
    assert "omnibase_infra#6039" in entry["description"]
    assert "overlay" in entry["description"]
    for check in entry["checks"]:
        assert check["check_type"] == "command"
        assert "--host h201" in check["check_value"]
        assert "stall_responder" in check["check_value"]
        assert "pytest" not in check["check_value"]


def test_ac3_is_bound_to_the_tests_that_consume_the_overlay() -> None:
    loaded = yaml.safe_load(OCC_CONTRACT.read_text(encoding="utf-8"))
    bound = [e for e in loaded["dod_evidence"] if "AC3" in e.get("binds_ac", [])]
    assert any(
        "required or bound or default_tick or env" in c["check_value"]
        for e in bound
        for c in e["checks"]
    )
