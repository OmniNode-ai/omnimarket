# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Every test in this directory reads the synthetic overlay tree unless it sets its own."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_idle_slot_fill_effect.handlers import (
    handler_idle_slot_fill,
    handler_ruling_recovery,
    ruling_history,
)
from tests.helpers.fake_ledger_rows import FakeLedgerRows
from tests.nodes.node_idle_slot_fill_effect.overlay_fixture import ENV


@pytest.fixture(autouse=True)
def _overlay_roots(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", ENV["ONEX_SKILL_OVERLAY_ROOTS"])
    # The throughput effect now reads ruling history every tick, through the ledger reader node.
    # Older fill scenarios have no ruled work; no scenario spawns the reader or consults a ledger file.
    monkeypatch.setattr(
        ruling_history,
        "SubprocessLedgerRowsReader",
        lambda _environ, **_kw: FakeLedgerRows(),
    )
    # The fixer-HOLD read goes through the ledger reader node, never a file: no scenario spawns it.
    monkeypatch.setattr(
        handler_idle_slot_fill,
        "SubprocessLedgerRowsReader",
        lambda _environ, **_kw: FakeLedgerRows(),
    )
    monkeypatch.setattr(
        handler_ruling_recovery,
        "read_check",
        lambda check: (
            list(check["model_ids"])
            if check["kind"] == "review-voters"
            else [{"project": "fixture", "service": "runtime"}]
        ),
    )
