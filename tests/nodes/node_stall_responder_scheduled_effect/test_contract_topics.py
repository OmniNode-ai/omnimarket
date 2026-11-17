# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The terminal event node_stall_responder_scheduled_effect declares is a topic it publishes (OMN-25160)."""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.nodes.node_stall_responder_scheduled_effect.handlers.handler_stall_responder_scheduled import (
    HandlerStallResponderScheduled,
)
from omnimarket.nodes.node_stall_responder_scheduled_effect.handlers.stall_responder_tick import (
    ModelStallResponderTickOutcome,
)

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
NODE = "node_stall_responder_scheduled_effect"
HANDLER_LOGGER = (
    "omnimarket.nodes.node_stall_responder_scheduled_effect.handlers."
    "handler_stall_responder_scheduled"
)


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
        # onex lab-work run is refused by the broker ACL; the check names the ssh route that runs.
        assert "onex lab-work run" not in check["check_value"]
        assert "onex_lab_run.py" in check["check_value"]
        assert "--via ssh" in check["check_value"]


def _ac4_pattern() -> re.Pattern[str]:
    loaded = yaml.safe_load(OCC_CONTRACT.read_text(encoding="utf-8"))
    (check,) = [
        c
        for e in loaded["dod_evidence"]
        if "AC4" in e.get("binds_ac", [])
        for c in e["checks"]
    ]
    found = re.search(r'grep -E "([^"]+)"', check["check_value"])
    assert found is not None, "the AC4 check matches one grep -E pattern"
    return re.compile(found.group(1))


def _runtime_line(record: logging.LogRecord) -> str:
    """The line the dev runtime writes for a record (the h201 container log format)."""
    return f"2026-11-17 22:07:13 [{record.levelname}] {record.name}: {record.getMessage()}"


def _schedule_run_lines(
    caplog: pytest.LogCaptureFixture, outcome: str, tmp_path: Path
) -> list[str]:
    tick_outcome = ModelStallResponderTickOutcome(
        outcome == "failed", "stall_rows=2 dispatches=3 outcome=completed"
    )
    (tmp_path / NODE).mkdir()
    (tmp_path / NODE / "overlay.yaml").write_text(
        yaml.safe_dump(
            {
                "job_id": "stall_responder",
                "run_interval_seconds": 600,
                "grace_seconds": 600,
                "window_seconds": 3600,
                "claim_lookback_seconds": 86400,
                "redispatch_after_hours": 24,
                "ledger_read": {
                    "route": "command",
                    "command_env": "ONEX_LEDGER_BUS_READ_COMMAND",
                    "timeout_seconds": 120,
                    "row_limit": 20000,
                },
            }
        ),
        encoding="utf-8",
    )
    handler = HandlerStallResponderScheduled(
        environ={"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)},
        tick=lambda **_: tick_outcome,
    )
    now = datetime(2026, 11, 17, 22, 7, 13, tzinfo=UTC)
    with caplog.at_level(logging.INFO, logger=HANDLER_LOGGER):
        handler.handle(
            ModelRuntimeTick(
                now=now,
                tick_id=uuid4(),
                sequence_number=1,
                scheduled_at=now,
                correlation_id=uuid4(),
                scheduler_id="runtime-fixture",
                tick_interval_ms=1000,
            )
        )
    return [
        _runtime_line(r)
        for r in caplog.records
        if r.name == HANDLER_LOGGER and "schedule-run" in r.getMessage()
    ]


def test_ac4_check_matches_the_schedule_run_line_this_node_logs_for_a_completed_run(
    caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    lines = _schedule_run_lines(caplog, "completed", tmp_path)
    assert len(lines) == 1
    assert _ac4_pattern().search(lines[0])


def test_ac4_check_does_not_match_a_failed_run_whose_detail_says_completed(
    caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    lines = _schedule_run_lines(caplog, "failed", tmp_path)
    assert len(lines) == 1
    assert "completed" in lines[0]
    assert not _ac4_pattern().search(lines[0])


@pytest.mark.parametrize(
    "line",
    [
        # Lines read on the h201 runtime log at 2026-11-17T22:10Z, and lines that hold both strings but are not a
        # schedule-run line of this job with outcome completed.
        "2026-11-17 22:07:13 [INFO] omnibase_infra.runtime.service_dispatch_result_applier: Published output event to onex.evt.omnimarket.schedule-run.v1 (correlation_id=90b77655-9a67-4158-9fb4-123f812a720a)",
        "2026-11-17 22:07:43 [INFO] aiokafka.consumer.subscription_state: Updating subscribed topics to: frozenset({'onex.evt.omnimarket.work-ledger-emit-completed.v1', 'node_stall_responder_scheduled_effect stall_responder'})",
        "2026-11-17 22:07:13 [ERROR] omnimarket.handlers.scheduled_overlay_refusal: scheduled overlay refused: node=node_stall_responder_scheduled_effect error_type=NodeOverlayError detail=stall_responder completed",
        "2026-11-17 22:07:13 [INFO] omnimarket.nodes.node_stall_responder_scheduled_effect.handlers.handler_stall_responder_scheduled: schedule-run job_id=stall_responder outcome=failed detail=completed",
        "2026-11-17 22:07:13 [INFO] omnimarket.nodes.node_stall_responder_scheduled_effect.handlers.handler_stall_responder_scheduled: schedule-run job_id=stall_responder_canary outcome=completed detail=",
        "2026-11-17 22:07:13 [INFO] omnimarket.nodes.node_ticket_closer_scheduled_effect.handlers.handler_ticket_closer_scheduled: schedule-run job_id=stall_responder outcome=completed detail=",
    ],
)
def test_ac4_check_does_not_match_a_line_that_is_not_the_completed_run_of_the_job(
    line: str,
) -> None:
    assert "schedule-run" in line or (
        "stall_responder" in line and "completed" in line
    )
    assert not _ac4_pattern().search(line)


def test_ac3_is_bound_to_the_tests_that_consume_the_overlay() -> None:
    loaded = yaml.safe_load(OCC_CONTRACT.read_text(encoding="utf-8"))
    bound = [e for e in loaded["dod_evidence"] if "AC3" in e.get("binds_ac", [])]
    assert any(
        "required or bound or default_tick or env" in c["check_value"]
        for e in bound
        for c in e["checks"]
    )
