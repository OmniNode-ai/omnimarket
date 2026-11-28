# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The OMN-26115 AC3 floor evidence tracks the omnibase-core floor, not one literal.

The contract's AC3 floor check once grepped for the literal string
``omnibase-core>=0.47.74,<0.48.0``, which went red when dev raised both floors to
0.47.75. The check now accepts any floor at or above 0.47.74 (the first core that
carries ``ModelRunNodeHosted.run``) and still refuses a floor below it.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACT = REPO_ROOT / "contracts" / "OMN-26115.yaml"


def floor_commands(contract_text: str) -> list[str]:
    """Every command check, bound to AC3, that reads an omnibase-core floor."""
    data = yaml.safe_load(contract_text)
    commands: list[str] = []
    for item in data["dod_evidence"]:
        if "AC3" not in item.get("binds_ac", []):
            continue
        for check in item["checks"]:
            if (
                check["check_type"] == "command"
                and "omnibase-core" in check["check_value"]
            ):
                commands.append(check["check_value"])
    return commands


def _run(command: str, root: Path) -> int:
    if command.startswith("python3 -I -c '"):
        # `python3 -I -c '<source>'`: take the quoted source whole.
        source = command.split("-c '", 1)[1].rsplit("'", 1)[0]
        argv = [sys.executable, "-I", "-c", source]
        return subprocess.run(
            argv, cwd=root, check=False, capture_output=True
        ).returncode
    return subprocess.run(
        ["sh", "-c", command], cwd=root, check=False, capture_output=True
    ).returncode


def test_ac3_floor_checks_exist() -> None:
    assert floor_commands(CONTRACT.read_text())


def test_ac3_floor_checks_pass_on_the_current_floor() -> None:
    for command in floor_commands(CONTRACT.read_text()):
        assert _run(command, REPO_ROOT) == 0, command


def test_ac3_floor_check_refuses_a_floor_below_the_first_carrying_core(
    tmp_path: Path,
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\ndependencies = [\n    "omnibase-core>=0.47.73,<0.48.0",\n'
        '    "omnibase-core>=0.47.73,<0.48.0",\n]\n'
    )
    commands = [
        c
        for c in floor_commands(CONTRACT.read_text())
        if c.startswith("python3 -I -c '")
    ]
    assert commands
    for command in commands:
        assert _run(command, tmp_path) == 1, command
