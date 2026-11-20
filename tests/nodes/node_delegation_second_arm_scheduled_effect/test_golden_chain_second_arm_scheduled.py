# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chains of the second arm's tick on the runtime schedule."""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.nodes.node_delegation_second_arm_scheduled_effect.handlers.handler_second_arm_scheduled import (
    HandlerSecondArmScheduled,
)

pytestmark = pytest.mark.unit

T0 = datetime(2026, 11, 12, 23, 0, tzinfo=UTC)
NODE = "node_delegation_second_arm_scheduled_effect"
BACKEND = "local-omnipc2-chat"


def tick(at: datetime) -> ModelRuntimeTick:
    return ModelRuntimeTick(
        now=at,
        tick_id=uuid4(),
        sequence_number=1,
        scheduled_at=at,
        correlation_id=uuid4(),
        scheduler_id="runtime-fixture",
        tick_interval_ms=1000,
    )


def environ(tmp_path: Path) -> dict[str, str]:
    node_dir = tmp_path / NODE
    node_dir.mkdir()
    (node_dir / "overlay.yaml").write_text(
        yaml.safe_dump(
            {
                "job_id": "second_arm",
                "run_interval_seconds": 300,
                "grace_seconds": 60,
                "backend_id": BACKEND,
                "task_type": "research",
                "prompt": "Say OK.",
                "delegate_command": ["onex", "delegate"],
                "transport_args": [
                    "--bus",
                    "kafka",
                    "--lane",
                    "dev",
                    "--locus",
                    "deployed-lane",
                ],
                "timeout_seconds": 60,
            }
        ),
        encoding="utf-8",
    )
    return {"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)}


def receipt(*, status: str = "completed", backend_id: str = BACKEND) -> str:
    return (
        json.dumps(
            {
                "run_id": "r1",
                "result": {
                    "terminal_payload": {
                        "payload": {
                            "status": status,
                            "model_name": "qwen",
                            "attempts": [{"backend_id": backend_id}],
                        }
                    }
                },
            }
        )
        + "\n"
    )


class _Runner:
    def __init__(self, result: tuple[int, str, str] | None = None) -> None:
        self.result = result or (0, receipt(), "")
        self.calls: list[list[str]] = []

    def __call__(self, argv: Sequence[str], timeout: int) -> tuple[int, str, str]:
        self.calls.append(list(argv))
        return self.result


def test_golden_a_due_tick_runs_the_pinned_delegate_and_returns_its_event(
    tmp_path: Path,
) -> None:
    runner = _Runner()
    event = HandlerSecondArmScheduled(environ=environ(tmp_path), runner=runner).handle(
        tick(T0)
    )
    assert event is not None
    assert (event.job_id, event.node_name, event.outcome) == (
        "second_arm",
        NODE,
        "completed",
    )
    assert f"backend_id={BACKEND}" in event.detail
    assert len(runner.calls) == 1
    argv = runner.calls[0]
    assert argv[:2] == ["onex", "delegate"]
    assert argv[-3:] == ["--backend-id", BACKEND, "--json"]
    assert "--lane" in argv
    assert argv[argv.index("--lane") + 1] == "dev"


def test_golden_the_gate_runs_once_per_interval(tmp_path: Path) -> None:
    runner = _Runner()
    handler = HandlerSecondArmScheduled(environ=environ(tmp_path), runner=runner)
    assert handler.handle(tick(T0)) is not None
    assert handler.handle(tick(T0 + timedelta(seconds=299))) is None
    assert handler.handle(tick(T0 + timedelta(seconds=300))) is not None
    assert len(runner.calls) == 2


def test_error_no_overlay_refuses_once_and_returns_none(
    caplog: pytest.LogCaptureFixture,
) -> None:
    handler = HandlerSecondArmScheduled(environ={})
    with caplog.at_level(logging.ERROR):
        assert handler.handle(tick(T0)) is None
        assert handler.handle(tick(T0 + timedelta(seconds=1))) is None
    errors = [record for record in caplog.records if record.levelname == "ERROR"]
    assert len(errors) == 1
    assert NODE in errors[0].message


def test_error_output_without_a_receipt_is_a_failed_event(tmp_path: Path) -> None:
    runner = _Runner((0, "no json here", ""))
    event = HandlerSecondArmScheduled(environ=environ(tmp_path), runner=runner).handle(
        tick(T0)
    )
    assert event is not None
    assert event.outcome == "failed"
    assert "printed no receipt" in event.detail


def test_error_a_different_backend_answering_means_the_pin_was_not_honoured(
    tmp_path: Path,
) -> None:
    runner = _Runner((0, receipt(backend_id="local-coder"), ""))
    event = HandlerSecondArmScheduled(environ=environ(tmp_path), runner=runner).handle(
        tick(T0)
    )
    assert event is not None
    assert event.outcome == "failed"
    assert "the pin was not honoured" in event.detail


def test_error_a_failed_receipt_is_a_failed_event(tmp_path: Path) -> None:
    runner = _Runner((0, receipt(status="failed"), ""))
    event = HandlerSecondArmScheduled(environ=environ(tmp_path), runner=runner).handle(
        tick(T0)
    )
    assert event is not None
    assert event.outcome == "failed"


def test_control_a_timed_out_delegate_is_a_failed_event(tmp_path: Path) -> None:
    runner = _Runner((124, "", "exceeded"))
    event = HandlerSecondArmScheduled(environ=environ(tmp_path), runner=runner).handle(
        tick(T0)
    )
    assert event is not None
    assert event.outcome == "failed"
