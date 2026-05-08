# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
# onex-allow-file OMN-10580 reason="test fixture — uses lab IP and a redacted-test-placeholder password literal to verify the script redacts secrets from output; values are test inputs, not credentials"
"""Tests for scripts/run_delegation_cost_projection_process.sh."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = REPO_ROOT / "scripts" / "run_delegation_cost_projection_process.sh"


def _base_env(env_file: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["OMNIMARKET_PROJECTION_ENV_FILE"] = str(env_file)
    env.pop("OMNIDASH_ANALYTICS_DB_URL", None)
    env.pop("KAFKA_BROKERS", None)
    env.pop("KAFKA_BOOTSTRAP_SERVERS", None)
    return env


@pytest.mark.unit
def test_script_has_valid_bash_syntax() -> None:
    result = subprocess.run(
        ["bash", "-n", str(SCRIPT_PATH)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.unit
def test_check_fails_when_env_file_missing(tmp_path: Path) -> None:
    missing_env = tmp_path / "missing.env"

    result = subprocess.run(
        ["bash", str(SCRIPT_PATH), "--check"],
        cwd=REPO_ROOT,
        env=_base_env(missing_env),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "env file is missing" in result.stderr


@pytest.mark.unit
def test_check_accepts_local_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / "local.env"
    env_file.write_text(
        "\n".join(
            [
                "OMNIDASH_ANALYTICS_DB_URL=postgresql://postgres:secret@localhost:5432/omnibase_infra",
                "KAFKA_BROKERS=localhost:19092",
            ]
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        ["bash", str(SCRIPT_PATH), "--check"],
        cwd=REPO_ROOT,
        env=_base_env(env_file),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "delegation-cost projection preflight ok" in result.stdout
    assert "secret" not in result.stdout
    assert "secret" not in result.stderr


@pytest.mark.unit
def test_check_refuses_protected_runtime_without_printing_secret(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / "protected.env"
    env_file.write_text(
        "\n".join(
            [
                "OMNIDASH_ANALYTICS_DB_URL=postgresql://postgres:redacted-test-placeholder@192.168.86.201:5436/omnibase_infra",  # onex-allow-internal-ip: testing that the script rejects LAN DSNs
                "KAFKA_BROKERS=localhost:19092",
            ]
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        ["bash", str(SCRIPT_PATH), "--check"],
        cwd=REPO_ROOT,
        env=_base_env(env_file),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "protected .201 runtime" in result.stderr
    assert "redacted-test-placeholder" not in result.stdout
    assert "redacted-test-placeholder" not in result.stderr
