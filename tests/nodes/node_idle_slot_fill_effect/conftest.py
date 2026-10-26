# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Every test in this directory reads the synthetic overlay tree unless it sets its own."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.nodes.node_idle_slot_fill_effect.overlay_fixture import ENV


@pytest.fixture(autouse=True)
def _overlay_roots(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", ENV["ONEX_SKILL_OVERLAY_ROOTS"])


@pytest.fixture(autouse=True)
def _empty_fixer_ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ledger = tmp_path / "empty-ledger.md"
    ledger.write_text("", encoding="utf-8")
    monkeypatch.setenv("ONEX_LEDGER_PATH", str(ledger))
