# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Live-broker seam test for the experiment result record -> projection chain.

The seam is the one this change made: the record node publishes a recorded
terminal on the success topic, and the projection node consumes it and writes
a row to the snapshot topic. This test drives both through the real dev broker
using the commands named by environment variables.

It runs where a lab host names the commands in
``EXPERIMENT_RESULT_LIVE_RECORD_COMMAND`` and ``EXPERIMENT_RESULT_LIVE_READ_COMMAND``.
It SKIPS without those variables.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from omnimarket.events.experiment_arm_result import ModelExperimentArmResult

pytestmark = [pytest.mark.integration, pytest.mark.kafka]

RECORD_COMMAND_ENV = "EXPERIMENT_RESULT_LIVE_RECORD_COMMAND"
READ_COMMAND_ENV = "EXPERIMENT_RESULT_LIVE_READ_COMMAND"
POLL_ATTEMPTS_ENV = "EXPERIMENT_RESULT_LIVE_POLL_ATTEMPTS"
POLL_INTERVAL_ENV = "EXPERIMENT_RESULT_LIVE_POLL_INTERVAL_S"

SNAPSHOT_TOPIC = "onex.snapshot.projection.eval.experiment-results.v1"


def _require_live_commands() -> tuple[str, str, int, float]:
    """Return (record_cmd, read_cmd, attempts, interval) or skip."""
    record_cmd = os.environ.get(RECORD_COMMAND_ENV)
    read_cmd = os.environ.get(READ_COMMAND_ENV)
    if not record_cmd or not read_cmd:
        pytest.skip(
            f"{RECORD_COMMAND_ENV} and {READ_COMMAND_ENV} name no live commands on this host"
        )
    attempts_raw = os.environ.get(POLL_ATTEMPTS_ENV)
    interval_raw = os.environ.get(POLL_INTERVAL_ENV)
    if not attempts_raw or not interval_raw:
        pytest.skip(
            f"{POLL_ATTEMPTS_ENV} and {POLL_INTERVAL_ENV} are required when the live commands are set"
        )
    return record_cmd, read_cmd, int(attempts_raw), float(interval_raw)


def _valid_result(experiment_id: str) -> ModelExperimentArmResult:
    return ModelExperimentArmResult(
        tenant_id=uuid4(),
        experiment_id=experiment_id,
        arm="arm-a",
        model_id="model-x",
        engine="gpt4",
        n=10,
        correct=7,
        wilson_low=0.55,
        wilson_high=0.85,
        baseline_accuracy=0.5,
        latency_p50_ms=100.0,
        latency_p90_ms=200.0,
        gpu_seconds=5.0,
        corpus_sha256="a" * 64,
        truth_sha256="b" * 64,
        measured_at=datetime.now(UTC),
        report_ref="",
    )


def _mismatched_result(experiment_id: str) -> ModelExperimentArmResult:
    return ModelExperimentArmResult(
        tenant_id=uuid4(),
        experiment_id=experiment_id,
        arm="arm-b",
        model_id="model-y",
        engine="gpt4",
        n=10,
        correct=7,
        wilson_low=0.01,
        wilson_high=0.02,
        baseline_accuracy=0.5,
        latency_p50_ms=100.0,
        latency_p90_ms=200.0,
        gpu_seconds=5.0,
        corpus_sha256="a" * 64,
        truth_sha256="b" * 64,
        measured_at=datetime.now(UTC),
        report_ref="",
    )


def _send_record_command(record_cmd: str, result: ModelExperimentArmResult) -> dict:
    """Send a record request to the dev bus and return the terminal JSON."""
    payload = json.dumps({"result": result.model_dump(mode="json")})
    proc = subprocess.run(
        record_cmd.split(),
        input=payload.encode("utf-8"),
        capture_output=True,
        timeout=60,
    )
    assert proc.returncode == 0, f"record command failed: {proc.stderr.decode()}"
    return json.loads(proc.stdout.decode("utf-8"))


def _read_snapshot_rows(read_cmd: str) -> list[dict]:
    """Read the snapshot topic and return the list of row dicts."""
    proc = subprocess.run(
        read_cmd.split(),
        capture_output=True,
        timeout=60,
    )
    assert proc.returncode == 0, f"read command failed: {proc.stderr.decode()}"
    rows = []
    for line in proc.stdout.decode("utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def _poll_for_row(
    read_cmd: str,
    experiment_id: str,
    attempts: int,
    interval: float,
) -> dict | None:
    """Poll the read command until a row with the given experiment_id appears."""
    for _ in range(attempts):
        rows = _read_snapshot_rows(read_cmd)
        for row in rows:
            if row.get("experiment_id") == experiment_id:
                return row
        time.sleep(interval)
    return None


def test_live_record_then_projection_appears() -> None:
    """Record a valid result and assert the projection row appears in the snapshot."""
    record_cmd, read_cmd, attempts, interval = _require_live_commands()
    experiment_id = f"seam-probe-{uuid4().hex}"
    result = _valid_result(experiment_id)

    terminal = _send_record_command(record_cmd, result)
    # The terminal must be the completed one
    assert terminal.get("status") == "completed", (
        f"expected completed terminal, got {terminal}"
    )

    row = _poll_for_row(read_cmd, experiment_id, attempts, interval)
    assert row is not None, (
        f"no snapshot row with experiment_id={experiment_id} appeared within {attempts} attempts"
    )

    # Assert the derived fields match what the fold gives
    accuracy = result.correct / result.n
    assert row["accuracy"] == pytest.approx(accuracy)
    assert row["wilson_low"] == pytest.approx(result.wilson_low)
    assert row["wilson_high"] == pytest.approx(result.wilson_high)
    assert row["clears_baseline"] == (result.wilson_low > result.baseline_accuracy)


def test_live_mismatched_interval_fails_and_no_row() -> None:
    """Record a mismatched-interval result and assert the failed terminal and no snapshot row."""
    record_cmd, read_cmd, attempts, interval = _require_live_commands()
    experiment_id = f"seam-probe-{uuid4().hex}"
    result = _mismatched_result(experiment_id)

    terminal = _send_record_command(record_cmd, result)
    # The terminal must be the failed one
    assert terminal.get("status") == "failed", (
        f"expected failed terminal, got {terminal}"
    )
    refusal_codes = [r.get("refusal") for r in terminal.get("refusals", [])]
    assert "interval_mismatch" in refusal_codes, (
        f"expected interval_mismatch, got {refusal_codes}"
    )

    # No snapshot row should appear for this experiment_id
    row = _poll_for_row(read_cmd, experiment_id, attempts, interval)
    assert row is None, (
        f"unexpected snapshot row for failed experiment_id={experiment_id}"
    )
