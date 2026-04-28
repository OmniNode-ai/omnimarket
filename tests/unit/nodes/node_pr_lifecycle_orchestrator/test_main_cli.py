# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI tests for node_pr_lifecycle_orchestrator."""

from __future__ import annotations

import os
import subprocess
import sys
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
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    result = ModelPrLifecycleResult.model_validate_json(completed.stdout)
    assert result.correlation_id == command.correlation_id
    assert result.final_state == "COMPLETE"
    result_path = tmp_path / "state" / "merge-sweep" / command.run_id / "result.json"
    assert result_path.exists()
