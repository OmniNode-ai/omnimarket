# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Mutation falsifiers: each check is shown to bite by switching its mechanism off.

With the ruling gate disabled the planted ruling goes through, and with stale marking disabled
the stale read carries no marker. A refusal test that still passed under either mutant would be
vacuous, so these pin that the refusals and the markers come from the code they claim to test.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from omnimarket.nodes.node_handoff_check_effect.handlers import (
    assemble as assemble_module,
)
from omnimarket.nodes.node_handoff_check_effect.handlers.rulings import (
    account_rulings,
)
from omnimarket.nodes.node_handoff_check_effect.models import (
    ModelAccount,
    ModelFact,
    ModelLedgerRow,
    ModelRulingGatePolicy,
)

from .test_handoff_check import (
    CLAIM_OTHER,
    RULING,
    probe,
    run,
    write_ledger,
    write_probes,
)

pytestmark = pytest.mark.unit


def test_mutant_gate_disabled_lets_the_planted_ruling_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = write_ledger(tmp_path, RULING, CLAIM_OTHER)
    probes = write_probes(tmp_path, probe("dev-bus", True))
    assert run(tmp_path, ledger, probes) == 1  # control: the gate refuses

    real = account_rulings

    def gate_off(
        rows: Sequence[ModelLedgerRow],
        gate: ModelRulingGatePolicy,
        now: dt.datetime,
        declared: Mapping[str, Mapping[str, object]],
    ) -> list[ModelAccount]:
        accounts = real(rows, gate, now, declared)
        for account in accounts:
            account.problem = ""
        return accounts

    monkeypatch.setattr(assemble_module, "account_rulings", gate_off)
    assert run(tmp_path, ledger, probes) == 0


def test_mutant_stale_marking_disabled_drops_the_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = write_ledger(tmp_path, CLAIM_OTHER)
    probes = write_probes(
        tmp_path, probe("dev-bus", True, read_at="2026-11-01T22:00:00Z")
    )
    out_dir = tmp_path / "out"
    assert run(tmp_path, ledger, probes, "--out-dir", str(out_dir)) == 0
    assert "STALE" in next(out_dir.iterdir()).read_text(encoding="utf-8")  # control

    def stale_off(self: ModelFact, now: dt.datetime) -> bool:
        return False

    monkeypatch.setattr(ModelFact, "stale", stale_off)
    other = tmp_path / "out2"
    assert run(tmp_path, ledger, probes, "--out-dir", str(other)) == 0
    assert "STALE" not in next(other.iterdir()).read_text(encoding="utf-8")
