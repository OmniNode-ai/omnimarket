# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The review fixes that are about where code lives and what the contract proves (OMN-24700).

The probe command body lives in a handler module so the baselined ``cli_secret.py`` does not
grow, and the contract commands that prove the gateway change run without broker credentials.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[4]
_CLI_SECRET = _ROOT / "src/omnimarket/cli/cli_secret.py"
_PROBE_HANDLER = (
    _ROOT
    / "src/omnimarket/nodes/node_claude_review_gateway_effect/handlers/review_key_credit_probe.py"
)
_CODE_LINE_BUDGET = 1212  # the code lines cli_secret.py has at the landing branch


def _code_lines(path: Path) -> int:
    return sum(
        1
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    )


def test_cli_secret_is_within_its_baselined_code_line_budget() -> None:
    assert _code_lines(_CLI_SECRET) <= _CODE_LINE_BUDGET


def test_the_probe_command_body_lives_in_the_probe_handler_module() -> None:
    cli = _CLI_SECRET.read_text(encoding="utf-8")
    handler = _PROBE_HANDLER.read_text(encoding="utf-8")
    assert "def review_key_probe_command(" in handler
    assert "probe_keys(" in handler
    assert "probe_keys(" not in cli
    assert "key-probe refused" not in cli


def _contract_commands(contract: Path) -> list[str]:
    document = yaml.safe_load(contract.read_text(encoding="utf-8"))
    commands = [str(req["command"]) for req in document["evidence_requirements"]]
    for entry in document["dod_evidence"]:
        commands.extend(str(check["check_value"]) for check in entry["checks"])
    return [" ".join(command.split()) for command in commands]


def test_every_command_that_runs_the_whole_gateway_test_directory_excludes_kafka() -> (
    None
):
    directory = re.compile(
        r"pytest tests/unit/nodes/node_claude_review_gateway_effect -q"
    )
    runs = [
        command
        for command in _contract_commands(_ROOT / "contracts/OMN-24658.yaml")
        if directory.search(command)
    ]
    # The evidence requirement and the integration proof both run the directory.
    assert len(runs) == 2
    for command in runs:
        assert '-m "not kafka"' in command, command
