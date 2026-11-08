# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Review finding 5347:1: two independent asks from one lane in one second keep distinct commitment ids (OMN-23013)."""

from __future__ import annotations

from datetime import UTC, datetime, tzinfo
from pathlib import Path

import pytest

from omnimarket.models.commitment import ModelCommitmentLedgerRequest
from omnimarket.nodes.node_commitment_ledger_compute.handlers.handler_commitment_ledger import (
    HandlerCommitmentLedger,
)
from omnimarket.nodes.node_commitment_writer_effect import runtime_commitment_writer
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    evidence_row_from_text,
)
from tests.nodes.node_commitment_check_runner_effect.support import ledger_rules

pytestmark = pytest.mark.unit

EXAMPLE = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_commitment_ledger_compute/overlay.example.yaml"
)
FROZEN = datetime(2026, 11, 7, 9, 0, 0, tzinfo=UTC)


class _Frozen(datetime):
    @classmethod
    def now(cls, tz: tzinfo | None = None) -> datetime:
        del tz
        return FROZEN


def _write(tmp_path: Path, ask: str, capsys: pytest.CaptureFixture[str]) -> str:
    ask_file = tmp_path / "ask.txt"
    ask_file.write_text(ask, encoding="utf-8")
    code = runtime_commitment_writer.main(
        [
            "--lane",
            "orchestrator-31d9",
            "--owner",
            "host-bringup-1",
            "--check",
            "uv run onex host-read --host h053",
            "--ask-file",
            str(ask_file),
        ]
    )
    assert code == 0
    return capsys.readouterr().out.strip()


def test_5347_1_two_asks_written_in_one_second_fold_to_two_commitments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    commitment_rules = ledger_rules()
    monkeypatch.setenv("ONEX_COMMITMENT_OVERLAY", str(EXAMPLE))
    monkeypatch.setattr(runtime_commitment_writer, "datetime", _Frozen)
    lines = [
        _write(tmp_path, "Move h053 to the NVMe", capsys),
        _write(tmp_path, "Move h101 to the NVMe", capsys),
    ]
    rows = tuple(evidence_row_from_text(line) for line in lines)
    assert all(r is not None for r in rows)
    result = HandlerCommitmentLedger().handle(
        ModelCommitmentLedgerRequest(
            operation="fold",
            rules=commitment_rules,
            now=FROZEN,
            ledger_rows=tuple(r for r in rows if r is not None),
        )
    )
    assert result.malformed == ()
    assert len(result.commitments) == 2
    assert len({c.commitment_id for c in result.commitments}) == 2


def test_5347_1_failing_control_the_same_nonce_in_one_second_is_a_duplicate() -> None:
    commitment_rules = ledger_rules()

    def build(ask: str):
        return (
            HandlerCommitmentLedger()
            .handle(
                ModelCommitmentLedgerRequest(
                    operation="build_open_row",
                    rules=commitment_rules,
                    now=FROZEN,
                    lane="orchestrator-31d9",
                    ask=ask,
                    owner="host-bringup-1",
                    check="uv run onex host-read",
                    nonce="aaaa",
                )
            )
            .row
        )

    rows = tuple(evidence_row_from_text(build(a) or "") for a in ("one", "two"))
    result = HandlerCommitmentLedger().handle(
        ModelCommitmentLedgerRequest(
            operation="fold",
            rules=commitment_rules,
            now=FROZEN,
            ledger_rows=tuple(r for r in rows if r is not None),
        )
    )
    assert len(result.commitments) == 1
    assert any("duplicate id" in m for m in result.malformed)
