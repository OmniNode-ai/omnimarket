# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI tests for node_pr_lifecycle_orchestrator."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import pytest
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope

from omnimarket.nodes.node_pr_lifecycle_orchestrator.handlers.handler_pr_lifecycle_orchestrator import (
    ModelPrLifecycleResult,
    ModelPrLifecycleStartCommand,
)

_REPO_ROOT = Path(__file__).resolve().parents[4]
_EVENT_TYPE = "omnimarket.pr-lifecycle-orchestrator-start"

# OMN-14883: this test's subprocess is a FRESH interpreter that must import the
# whole omnimarket package tree before the CLI does any work at all, and that
# import is I/O-bound, not CPU-bound: measured 14.1s wall for just
# ``import omnimarket.nodes.node_pr_lifecycle_orchestrator`` on the .201
# gate-runner (2.0s user CPU — the rest is reading the package tree off the
# container's /data mount) against 2-6s on a warm macOS checkout.
#
# A hardcoded ``timeout=30`` therefore asserted host import speed, not CLI
# behavior, and it failed the governed pre-push selector on the gate-runner for
# every diff that escalates to the full suite. Calibrate instead: time a bare
# import of the same module in the same environment, then scale the CLI budget
# off that measurement, floored so a fast host keeps a sane lower bound.
#
# The factor is measured, not guessed. Paired runs on the gate-runner at load
# 13.8/32 cores:
#
#     bare import: 15.50s, 12.29s        full CLI subprocess: 47.42s, 40.07s
#
# i.e. the CLI costs ~3.3x its own import (the CLI pays that import and then
# does its inventory work). The factor doubles that observed ratio so a warm
# page cache during the calibration — which makes the measured import cheaper
# than the one the CLI subsequently pays — cannot re-arm this failure. The
# floor is raised past the old 30s constant for the same reason: on the
# gate-runner the CLI's real cost is 40-47s, so a 30s floor could still win
# over a warm calibration and time out a healthy run.
_CLI_TIMEOUT_FLOOR_SECONDS = 60.0
_CLI_TIMEOUT_IMPORT_FACTOR = 6.0
# Hang guard on the calibration itself — not a budget, just a bound on a wedged
# interpreter, generous against the worst case measured above.
_IMPORT_CALIBRATION_CEILING_SECONDS = 300.0


def _cli_timeout_seconds() -> float:
    """Scale the CLI subprocess budget off this environment's measured import."""
    started = time.monotonic()
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import omnimarket.nodes.node_pr_lifecycle_orchestrator",
        ],
        capture_output=True,
        check=True,
        cwd=_REPO_ROOT,
        timeout=_IMPORT_CALIBRATION_CEILING_SECONDS,
    )
    import_seconds = time.monotonic() - started
    return max(_CLI_TIMEOUT_FLOOR_SECONDS, import_seconds * _CLI_TIMEOUT_IMPORT_FACTOR)


@pytest.mark.unit
def test_input_envelope_round_trips_to_pr_lifecycle_result(tmp_path: Path) -> None:
    """The module CLI accepts the contract event envelope and emits a result."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    gh = fake_bin / "gh"
    gh.write_text("#!/usr/bin/env bash\nexit 0\n")
    gh.chmod(0o755)

    command = ModelPrLifecycleStartCommand(
        correlation_id=uuid4(),
        run_id="omn-10166-cli",
        dry_run=True,
        inventory_only=True,
    )
    envelope = ModelEventEnvelope[ModelPrLifecycleStartCommand](
        event_type=_EVENT_TYPE,
        correlation_id=command.correlation_id,
        payload=command,
    )

    env = {
        **os.environ,
        "ONEX_STATE_DIR": str(tmp_path / "state"),
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
    }
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "omnimarket.nodes.node_pr_lifecycle_orchestrator",
            "--input",
            envelope.model_dump_json(),
        ],
        capture_output=True,
        check=False,
        cwd=_REPO_ROOT,
        env=env,
        text=True,
        timeout=_cli_timeout_seconds(),
    )

    assert completed.returncode == 0, completed.stderr
    result = ModelPrLifecycleResult.model_validate_json(completed.stdout)
    assert result.correlation_id == command.correlation_id
    assert result.final_state == "COMPLETE"
    result_path = tmp_path / "state" / "merge-sweep" / command.run_id / "result.json"
    assert result_path.exists()
