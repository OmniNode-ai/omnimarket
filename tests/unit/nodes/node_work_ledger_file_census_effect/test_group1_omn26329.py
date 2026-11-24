# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Census group 1: the group's files stop naming the ledger file and their census rows are lowered.

Each case reads the live tree, so it fails on the tree before this change (the files still hold a counted reader
or writer and a row of the shipped ratchet) and passes once they are moved and the rows are lowered.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_work_ledger_file_census_effect.handlers.classify import (
    executable_hits,
)
from omnimarket.nodes.node_work_ledger_file_census_effect.models import (
    ModelCensusOverlay,
)

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[4]
PACKAGE = ROOT / "src/omnimarket/nodes/node_work_ledger_file_census_effect"
RATCHET = PACKAGE / "ratchet.yaml"
NODES = "src/omnimarket/nodes"

#: Group 1 files cleared of every counted hit by this change (their rows are dropped).
CLEARED = (
    f"{NODES}/node_floor_simple_effect/contract.yaml",
    f"{NODES}/node_friction_rollup_tick_effect/runtime_friction_rollup_tick.py",
    f"{NODES}/node_handoff_check_effect/contract.yaml",
    f"{NODES}/node_handoff_check_effect/handlers/facts.py",
    f"{NODES}/node_handoff_check_effect/handlers/handler_handoff_check.py",
    f"{NODES}/node_handoff_check_effect/runtime_handoff_check_cli.py",
    f"{NODES}/node_host_reconcile_effect/__main__.py",
    f"{NODES}/node_hourly_tick_effect/models/model_hourly_tick.py",
)
LOWERED = f"{NODES}/node_host_installer_registry_units/contract.yaml"
#: The registry units contract held eight counted hits before this change; the rest stay for a later change.
LOWERED_ROW_BEFORE = 8


def _overlay() -> ModelCensusOverlay:
    data = yaml.safe_load((PACKAGE / "overlay.example.yaml").read_text())
    return ModelCensusOverlay.model_validate(data)


def _rows() -> dict[str, int]:
    rows: dict[str, int] = yaml.safe_load(RATCHET.read_text())["files"]["omnimarket"]
    return rows


@pytest.mark.parametrize("path", CLEARED)
def test_a_cleared_file_holds_no_executable_ledger_file_read_or_write(
    path: str,
) -> None:
    text = (ROOT / path).read_text(encoding="utf-8")
    hits = executable_hits(path, text, _overlay())
    assert hits == (), [f"{h.kind.value} {h.path}:{h.line}" for h in hits]


@pytest.mark.parametrize("path", CLEARED)
def test_a_cleared_file_has_no_ratchet_row(path: str) -> None:
    assert path not in _rows()


def test_the_registry_units_contract_row_is_lowered_never_raised() -> None:
    text = (ROOT / LOWERED).read_text(encoding="utf-8")
    live = len(executable_hits(LOWERED, text, _overlay()))
    row = _rows()[LOWERED]
    assert live <= row < LOWERED_ROW_BEFORE


LOWERED_HANDLER = (
    f"{NODES}/node_host_installer_registry_units/handlers/handler_registry_units.py"
)
#: The registry units handler held three counted hits before the dead {ledger_path} default was deleted.
LOWERED_HANDLER_ROW_BEFORE = 3


def test_the_registry_units_handler_row_is_lowered_never_raised() -> None:
    text = (ROOT / LOWERED_HANDLER).read_text(encoding="utf-8")
    live = len(executable_hits(LOWERED_HANDLER, text, _overlay()))
    row = _rows()[LOWERED_HANDLER]
    assert live <= row < LOWERED_HANDLER_ROW_BEFORE
