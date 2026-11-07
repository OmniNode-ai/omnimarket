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


def test_the_writer_prints_a_withdrawal_row_for_a_commitment_id(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ONEX_COMMITMENT_OVERLAY", str(EXAMPLE))
    cid = "2026-11-07T09:00:00Z-orchestrator-31d9"
    assert main(["--lane", "orchestrator-31d9", "--withdraw", cid]) == 0
    (line,) = capsys.readouterr().out.splitlines()
    assert f"commitment={cid}" in line
    assert "status=withdrawn" in line
    assert grammar.row_refusals(line) == []


def test_failing_control_a_withdrawal_of_no_commitment_prints_no_row(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ONEX_COMMITMENT_OVERLAY", str(EXAMPLE))
    assert main(["--lane", "orchestrator-31d9", "--withdraw", "not-an-id"]) == 65
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "not a commitment id" in captured.err


def test_the_writer_finds_the_ledger_nodes_overlay_under_the_overlay_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The rules are node_commitment_ledger_compute's overlay, found by that id, not by this node's name."""
    root = tmp_path / "roots"
    (root / "node_commitment_ledger_compute").mkdir(parents=True)
    (root / "node_commitment_ledger_compute" / "overlay.yaml").write_text(
        EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8"
    )
    monkeypatch.delenv("ONEX_COMMITMENT_OVERLAY", raising=False)
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(root))
    assert (
        main(["--lane", "orchestrator-31d9", "--withdraw", "2026-11-07T09:00:00Z-x"])
        == 0
    )
    assert "status=withdrawn" in capsys.readouterr().out
