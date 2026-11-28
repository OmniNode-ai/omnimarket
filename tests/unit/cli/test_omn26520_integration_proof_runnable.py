# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The OMN-26520 lab integration-proof item can run on the lab.

The lab refuses a work unit whose argv starts with an executable outside its
allowlist (``env`` among them), and ``onex lab-work run`` refuses to publish
without ``--bus-lane`` and ``--omnibase-path``. The item's command is read from
the ticket contract and judged against both.
"""

from __future__ import annotations

import shlex
from pathlib import Path

import yaml

from omnimarket.nodes.node_lab_work_unit_effect.handlers.handler_lab_work_unit_effect import (
    DEFAULT_ALLOWED_EXECUTABLES,
)

CONTRACT = Path(__file__).resolve().parents[3] / "contracts" / "OMN-26520.yaml"


def _lab_work_commands() -> list[list[str]]:
    contract = yaml.safe_load(CONTRACT.read_text())
    commands: list[list[str]] = []
    for item in contract["dod_evidence"]:
        for check in item.get("checks", []):
            if check.get("check_type") != "command":
                continue
            words = shlex.split(check["check_value"])
            if "lab-work" in words and "--" in words:
                commands.append(words)
    return commands


def test_the_work_unit_starts_with_an_executable_the_lab_runs() -> None:
    commands = _lab_work_commands()
    assert commands, "the contract binds no lab work unit"
    for words in commands:
        argv = words[words.index("--", words.index("lab-work")) + 1 :]
        assert argv[0] in DEFAULT_ALLOWED_EXECUTABLES, (
            f"the lab refuses {argv[0]!r}; allowed: "
            f"{sorted(DEFAULT_ALLOWED_EXECUTABLES)}"
        )


def test_the_command_names_the_dev_bus_lane_and_the_workspace_root() -> None:
    for words in _lab_work_commands():
        assert "--bus-lane" in words, "lab-work run names no bus lane"
        assert words[words.index("--bus-lane") + 1] == "dev"
        assert "--omnibase-path" in words, "lab-work run names no workspace root"
        assert words[0] == "onex", f"the command starts with {words[0]!r}, not onex"
