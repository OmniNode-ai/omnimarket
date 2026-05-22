# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Focused tests for the overseer verifier proof-of-life publisher."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from scripts import proof_of_life_overseer_verifier as proof


def test_command_topic_loaded_from_contract() -> None:
    assert proof._load_command_topic() == "onex.cmd.omnimarket.overseer-verify.v1"


def test_proof_cases_are_typed_wire_commands() -> None:
    cases = proof._build_cases()

    assert [case.name for case in cases] == [
        "pass",
        "low-confidence",
        "negative-cost",
        "bad-action",
    ]
    assert [case.expected_verdict for case in cases] == [
        "PASS",
        "ESCALATE",
        "ESCALATE",
        "FAIL",
    ]
    assert cases[2].command.cost_so_far == -0.5
    assert cases[3].command.allowed_actions == ["dispatch", "delete_all"]


def test_script_has_no_direct_handler_imports() -> None:
    source = Path(proof.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    handler_imports = [
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module is not None
        and "nodes" in node.module
        and "handlers" in node.module
    ]

    assert handler_imports == []


def test_main_dry_run_does_not_publish(capsys: pytest.CaptureFixture[str]) -> None:
    result = proof.main(["--case", "pass", "--dry-run"])

    captured = capsys.readouterr()
    assert result == 0
    assert "CASE: PASS - valid envelope" in captured.out
    assert "DRY RUN: no proof-of-life commands were published" in captured.out


def test_main_publishes_selected_case_with_contract_topic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published: dict[str, object] = {}

    async def fake_publish_commands(
        *,
        topic: str,
        commands: list[proof.ModelOverseerVerifyCommand],
        bootstrap_servers: str | None,
    ) -> None:
        published["topic"] = topic
        published["commands"] = commands
        published["bootstrap_servers"] = bootstrap_servers

    monkeypatch.setattr(proof, "_publish_commands", fake_publish_commands)

    result = proof.main(["--case", "bad-action", "--bootstrap", "localhost:9092"])

    assert result == 0
    assert published["topic"] == "onex.cmd.omnimarket.overseer-verify.v1"
    assert published["bootstrap_servers"] == "localhost:9092"
    commands = published["commands"]
    assert isinstance(commands, list)
    assert len(commands) == 1
    assert commands[0].task_id == "proof-of-life-bad-action"
