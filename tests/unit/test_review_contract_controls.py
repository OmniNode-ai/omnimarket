# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Executable controls for review evidence and version annotations."""

from __future__ import annotations

import ast
import os
import subprocess
from pathlib import Path

import pytest
import yaml
from omnibase_core.validation.scripts.python_ast_validator import PythonASTValidator
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

ROOT = Path(__file__).resolve().parents[2]
MODELS = (
    "src/omnimarket/nodes/node_session_bootstrap/models/model_session_contract.py",
    "src/omnimarket/nodes/node_session_post_mortem/models/model_post_mortem_report.py",
)


def _version_violations(source: str) -> list[object]:
    validator = PythonASTValidator("model_contract.py", source.splitlines())
    validator.visit(ast.parse(source))
    return [v for v in validator.violations if v.violation_type == "string_version"]


@pytest.mark.unit
@pytest.mark.parametrize("relative", MODELS)
def test_string_version_ok_on_real_telemetry_models(relative: str) -> None:
    source = (ROOT / relative).read_text()
    assert _version_violations(source) == []
    without_marker = "\n".join(
        line.replace("string-version-ok:", "removed-marker:")
        for line in source.splitlines()
    )
    assert _version_violations(without_marker)


@pytest.mark.unit
def test_string_version_ok_is_honoured_off_the_assignment_line() -> None:
    source = 'class Payload:\n    # string-version-ok: serialized telemetry\n    schema_version: str = "1.0"\n'
    assert _version_violations(source) == []
    assert _version_violations(source.replace("string-version-ok:", "removed-marker:"))


@pytest.mark.unit
@pytest.mark.parametrize("contract", ["OMN-19998", "OMN-25026"])
def test_diff_evidence_fails_when_origin_dev_is_unresolved(
    tmp_path: Path, contract: str
) -> None:
    data = yaml.safe_load((ROOT / "contracts" / f"{contract}.yaml").read_text())
    checks = [
        check["check_value"]
        for entry in data["dod_evidence"]
        for check in entry["checks"]
        if "git diff --name-only" in check["check_value"]
    ]
    assert checks
    env = scrub_git_location_env(os.environ)
    subprocess.run(["git", "init", "-q", str(tmp_path)], env=env, check=True)
    for command in checks:
        result = subprocess.run(
            ["bash", "-c", command],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0, result.stdout + result.stderr
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=gate",
                "-c",
                "user.email=gate@example.invalid",
                "commit",
                "--allow-empty",
                "-qm",
                "control",
            ],
            cwd=tmp_path,
            env=env,
            check=True,
        )
        subprocess.run(
            ["git", "update-ref", "refs/remotes/origin/dev", "HEAD"],
            cwd=tmp_path,
            env=env,
            check=True,
        )
        result = subprocess.run(
            ["bash", "-c", command],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stdout + result.stderr
