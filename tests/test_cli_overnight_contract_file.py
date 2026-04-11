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
