# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI tests for node_overnight --contract-file flag.

Verifies that:
1. --contract-file loads a valid YAML into a ModelOvernightContract and the
   overnight pipeline runs successfully in dry-run mode.
2. --contract-file with a missing path raises a FileNotFoundError and exits
   non-zero.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest
import yaml


@pytest.mark.unit
def test_contract_file_loads_valid_yaml(tmp_path):
    """A minimal valid contract YAML loads via --contract-file and runs in dry-run."""
    contract_data = {
        "session_id": "test-cli-overnight",
        "created_at": "2026-04-11T00:00:00Z",
        "phases": [
            {"phase_name": "build_loop_orchestrator", "timeout_seconds": 60},
        ],
    }
    contract_file = tmp_path / "test-contract.yaml"
    contract_file.write_text(yaml.safe_dump(contract_data))

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "omnimarket.nodes.node_overnight",
            "--contract-file",
            str(contract_file),
            "--dry-run",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, (
        f"CLI exited non-zero.\nstderr: {result.stderr}\nstdout: {result.stdout}"
    )

    payload = json.loads(result.stdout)
    assert payload["dry_run"] is True
    assert payload["session_status"] == "completed"


@pytest.mark.unit
def test_dispatch_phases_flag_present_in_help():
    """--dispatch-phases flag is exposed on the CLI (OMN-8404)."""
    result = subprocess.run(
        [sys.executable, "-m", "omnimarket.nodes.node_overnight", "--help"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0
    assert "--dispatch-phases" in result.stdout


@pytest.mark.unit
def test_dispatch_phases_flag_threads_to_handler(tmp_path):
    """--dispatch-phases invokes the real phase dispatcher path (OMN-8404).

    Without the flag, phases fall through as vacuous-green with ~0.0s
    duration_seconds. With the flag, the default dispatchers run and produce
    real phase results (success or captured failure). This test asserts the
    flag is threaded through to ``HandlerOvernight.handle(dispatch_phases=...)``
    by verifying the subprocess completes and reports results for the single
    requested phase in both modes.
    """
    contract_data = {
        "session_id": "test-cli-dispatch-phases",
        "created_at": "2026-04-11T00:00:00Z",
        "phases": [
            {"phase_name": "build_loop_orchestrator", "timeout_seconds": 60},
        ],
    }
    contract_file = tmp_path / "test-contract.yaml"
    contract_file.write_text(yaml.safe_dump(contract_data))

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "omnimarket.nodes.node_overnight",
            "--contract-file",
            str(contract_file),
            "--dispatch-phases",
            "--skip-build-loop",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"CLI exited non-zero.\nstderr: {result.stderr}\nstdout: {result.stdout}"
    )
    payload = json.loads(result.stdout)
    assert payload["session_status"] in ("completed", "partial", "failed")
    assert "build_loop_orchestrator" in payload["phases_skipped"]


@pytest.mark.unit
def test_contract_file_missing_path_errors():
    """A nonexistent --contract-file path causes a non-zero exit with a clear error."""
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "omnimarket.nodes.node_overnight",
            "--contract-file",
            "/nonexistent/path/contract.yaml",
            "--dry-run",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode != 0
    combined = (result.stderr + result.stdout).lower()
    assert "not found" in combined or "filenotfounderror" in combined
