# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The writer the ledger skills call: the ask verbatim in, one ledger row out, or a refusal naming why (OMN-23013 AC1)."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.handlers import ledger_row_grammar as grammar
from omnimarket.nodes.node_commitment_writer_effect.runtime_commitment_writer import (
    main,
)

pytestmark = pytest.mark.unit

EXAMPLE = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_commitment_ledger_compute/overlay.example.yaml"
)
ASK = 'Fix the Mac indexing | and do not stop at "most of it"'


def _argv(tmp_path: Path, ask: str, **changes: str) -> list[str]:
    ask_file = tmp_path / "ask.txt"
    ask_file.write_text(ask, encoding="utf-8")
    fields = {
        "--lane": "orchestrator-31d9",
        "--owner": "mac-indexing-1",
        "--check": "uv run onex index-status --expect fresh",
        "--ask-file": str(ask_file),
        **changes,
    }
    return [part for pair in fields.items() for part in pair]


def test_the_writer_prints_one_row_the_grammar_admits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ONEX_COMMITMENT_OVERLAY", str(EXAMPLE))
    assert main([*_argv(tmp_path, ASK), "--ticket", "OMN-23013"]) == 0
    out = capsys.readouterr().out
    (line,) = out.splitlines()
    assert " | STATUS | lane=orchestrator-31d9 | kind=commitment | " in line
    assert grammar.row_refusals(line) == []
    assert "|" not in line.split("ask=")[1].split(" | ")[0], "the ask's pipe is encoded"


def test_failing_control_an_ask_with_no_runnable_check_prints_no_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ONEX_COMMITMENT_OVERLAY", str(EXAMPLE))
    assert main(_argv(tmp_path, ASK, **{"--check": "   "})) == 65
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "the check is empty" in captured.err


def test_the_writer_refuses_with_no_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("ONEX_COMMITMENT_OVERLAY", raising=False)
    monkeypatch.delenv("ONEX_SKILL_OVERLAY_ROOTS", raising=False)
    assert main(_argv(tmp_path, ASK)) == 3
    assert "refused" in capsys.readouterr().err
