# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Durable acceptance rows survive refusals, escalation, and sink failures."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

import pytest

from omnimarket.nodes.node_pr_delegated_fix_effect.handlers.adapter_acceptance_telemetry import (
    EnumPlacementReason,
    JsonlAcceptanceTelemetryRecorder,
    ModelDelegatedFixAttemptRecord,
)

pytestmark = pytest.mark.unit


def _record(
    *,
    accepted: bool = False,
    outcome: str = "refused_denylist",
    task_type: str | None = "document",
    placement: EnumPlacementReason | None = None,
) -> ModelDelegatedFixAttemptRecord:
    return ModelDelegatedFixAttemptRecord(
        correlation_id=uuid4(),
        repo="OmniNode-ai/omnimarket",
        pr_number=500,
        block_reason="code_failure",
        task_type=task_type,
        delegation_model="test-model",
        outcome=outcome,
        accepted=accepted,
        placement_reason=placement,
        recorded_at=datetime.now(UTC),
    )


def test_roundtrip_preserves_accepted_rejected_and_escalated_samples(
    tmp_path: Path,
) -> None:
    recorder = JsonlAcceptanceTelemetryRecorder(tmp_path)
    records = [
        _record(
            accepted=True, outcome="accepted", placement=EnumPlacementReason.LOCAL_FIRST
        ),
        _record(),
        _record(
            accepted=True, outcome="accepted", placement=EnumPlacementReason.FALLBACK
        ),
        _record(outcome="gate_failed", task_type=None),
    ]
    assert recorder.read_samples() == []
    for record in records:
        recorder.record(record)
    reopened = JsonlAcceptanceTelemetryRecorder(tmp_path)
    assert reopened.read_samples() == records
    assert reopened.read_samples(task_type="document") == records[:3]
    assert reopened.read_samples(task_type="unseen") == []
    assert sum(row.accepted for row in reopened.read_samples()) == 2
    assert len(recorder.path.read_text(encoding="utf-8").splitlines()) == 4


def test_malformed_and_blank_rows_do_not_hide_valid_samples(tmp_path: Path) -> None:
    recorder = JsonlAcceptanceTelemetryRecorder(tmp_path)
    record = _record()
    recorder.record(record)
    with recorder.path.open("a", encoding="utf-8") as handle:
        handle.write('\nnot-json\n[]\n{"accepted": true}\n')
    recorder.record(record)
    assert recorder.read_samples() == [record, record]


def test_default_sink_uses_state_dir_then_omni_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ONEX_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("OMNI_HOME", str(tmp_path / "home"))
    assert JsonlAcceptanceTelemetryRecorder().path == (
        tmp_path / "state" / "delegated_fix" / "acceptance_telemetry.jsonl"
    )
    monkeypatch.delenv("ONEX_STATE_DIR")
    assert JsonlAcceptanceTelemetryRecorder().path == (
        tmp_path
        / "home"
        / ".onex_state"
        / "delegated_fix"
        / "acceptance_telemetry.jsonl"
    )
    monkeypatch.delenv("OMNI_HOME")
    with pytest.raises(RuntimeError, match="durable sink"):
        JsonlAcceptanceTelemetryRecorder()


def test_write_failure_is_best_effort(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory", encoding="utf-8")
    JsonlAcceptanceTelemetryRecorder(blocked).record(_record())
    assert "failed to record attempt" in caplog.text


def test_serialization_failure_is_best_effort(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(
        ModelDelegatedFixAttemptRecord,
        "model_dump_json",
        Mock(side_effect=ValueError("bad row")),
    )
    JsonlAcceptanceTelemetryRecorder(tmp_path).record(_record())
    assert "bad row" in caplog.text


def test_read_failure_returns_no_samples(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    recorder = JsonlAcceptanceTelemetryRecorder(tmp_path)
    recorder.record(_record())
    monkeypatch.setattr(Path, "read_text", Mock(side_effect=OSError("unreadable")))
    assert recorder.read_samples() == []
    assert "unreadable sink" in caplog.text
