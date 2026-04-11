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
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import yaml

from omnimarket.nodes.node_overnight import __main__ as overnight_main


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
    """--dispatch-phases is threaded to HandlerOvernight.handle (OMN-8404).

    Mocks HandlerOvernight so the test directly asserts the CLI passes
    ``dispatch_phases=True`` as a kwarg. Without mocking, a smoke-test based
    assertion can pass even if the CLI stops forwarding the flag, because
    vacuous-green and real-dispatch paths both return ``session_status``
    values. The inverse case (flag omitted → ``dispatch_phases=False``) is
    also asserted so a regression removing the forwarding is caught.
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

    fake_result = SimpleNamespace(
        session_status="completed",
        model_dump_json=lambda **_kwargs: '{"session_status":"completed"}',
    )

    # With --dispatch-phases → handle() called with dispatch_phases=True
    with (
        patch.object(overnight_main, "HandlerOvernight") as mocked_cls,
        patch.object(
            sys,
            "argv",
            [
                "node_overnight",
                "--contract-file",
                str(contract_file),
                "--dispatch-phases",
            ],
        ),
    ):
        mocked_cls.return_value.handle.return_value = fake_result
        overnight_main.main()

    _, kwargs = mocked_cls.return_value.handle.call_args
    assert kwargs["dispatch_phases"] is True

    # Without --dispatch-phases → handle() called with dispatch_phases=False
    with (
        patch.object(overnight_main, "HandlerOvernight") as mocked_cls,
        patch.object(
            sys,
            "argv",
            [
                "node_overnight",
                "--contract-file",
                str(contract_file),
            ],
        ),
    ):
        mocked_cls.return_value.handle.return_value = fake_result
        overnight_main.main()

    _, kwargs = mocked_cls.return_value.handle.call_args
    assert kwargs["dispatch_phases"] is False


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
