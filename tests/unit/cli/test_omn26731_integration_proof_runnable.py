# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The OMN-26731 lab integration-proof item names an executable the lab runs.

The lab host refuses a work unit whose argv starts with an executable outside
its allowlist (``env`` among them), so a Done-gate item that wraps its test
command in ``env`` can never run there. The item's command is read from the
ticket contract and its work-unit argv, the words after ``--``, is judged by
the lab handler's own refusal.
"""

from __future__ import annotations

import shlex
from pathlib import Path

import yaml

from omnimarket.nodes.node_lab_work_unit_effect.handlers.handler_lab_work_unit_effect import (
    DEFAULT_ALLOWED_EXECUTABLES,
)

CONTRACT = Path(__file__).resolve().parents[3] / "contracts" / "OMN-26731.yaml"


def _lab_work_argvs() -> list[list[str]]:
    contract = yaml.safe_load(CONTRACT.read_text())
    argvs: list[list[str]] = []
    for item in contract["dod_evidence"]:
        for check in item.get("checks", []):
            if check.get("check_type") != "command":
                continue
            words = shlex.split(check["check_value"])
            if "lab-work" in words and "--" in words:
                argvs.append(words[words.index("--", words.index("lab-work")) + 1 :])
    return argvs


def test_the_integration_proof_work_unit_starts_with_an_executable_the_lab_runs() -> (
    None
):
    argvs = _lab_work_argvs()
    assert argvs, "the contract binds no lab work unit"
    for argv in argvs:
        assert argv[0] in DEFAULT_ALLOWED_EXECUTABLES, (
            f"the lab refuses {argv[0]!r}; allowed: "
            f"{sorted(DEFAULT_ALLOWED_EXECUTABLES)}"
        )
