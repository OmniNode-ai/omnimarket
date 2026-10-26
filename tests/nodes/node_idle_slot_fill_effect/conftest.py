# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Every test in this directory reads the synthetic overlay tree unless it sets its own."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_idle_slot_fill_effect.handlers import handler_ruling_recovery
from tests.nodes.node_idle_slot_fill_effect.overlay_fixture import ENV


@pytest.fixture(autouse=True)
def _overlay_roots(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", ENV["ONEX_SKILL_OVERLAY_ROOTS"])
    # The throughput effect now reads ruling history every tick. Older fill
    # scenarios have no ruled work; never consult the developer's live ledger.
    ledger = tmp_path / "ROLLING_WORK_LEDGER.md"
    ledger.write_text("")
    monkeypatch.setenv("ONEX_LEDGER_PATH", str(ledger))
    monkeypatch.setattr(
        handler_ruling_recovery,
        "read_check",
        lambda check: (
            list(check["model_ids"])
            if check["kind"] == "review-voters"
            else [{"project": "fixture", "service": "runtime"}]
        ),
    )
