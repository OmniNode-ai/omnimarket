# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Census group 4, second part: the silent failure alarm handler and the work-state projection contract.

Each case reads the live tree, so it fails on the tree before this change (both files still hold a counted
reader and a row of the shipped ratchet) and passes once they are moved to the bus.
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

ALARM_HANDLER = (
    "src/omnimarket/nodes/node_silent_failure_alarm_effect/handlers/"
    "handler_silent_failure_alarm.py"
)
ALARM_IO = "src/omnimarket/nodes/node_silent_failure_alarm_effect/handlers/alarm_io.py"
PROJECTION_CONTRACT = (
    "src/omnimarket/nodes/node_work_state_projection_effect/contract.yaml"
)
PROJECTION_RUNTIME = (
    "src/omnimarket/nodes/node_work_state_projection_effect/runtime_work_state.py"
)
CLEARED = (ALARM_HANDLER, ALARM_IO, PROJECTION_CONTRACT, PROJECTION_RUNTIME)


def _overlay() -> ModelCensusOverlay:
    data = yaml.safe_load((PACKAGE / "overlay.example.yaml").read_text())
    return ModelCensusOverlay.model_validate(data)


@pytest.mark.parametrize("path", CLEARED)
def test_a_cleared_file_holds_no_executable_ledger_file_read_or_write(
    path: str,
) -> None:
    text = (ROOT / path).read_text(encoding="utf-8")
    hits = executable_hits(path, text, _overlay())
    assert hits == (), [f"{h.kind.value} {h.path}:{h.line}" for h in hits]


@pytest.mark.parametrize("path", [ALARM_HANDLER, PROJECTION_CONTRACT])
def test_a_cleared_file_has_no_ratchet_row(path: str) -> None:
    rows: dict[str, int] = yaml.safe_load(RATCHET.read_text())["files"]["omnimarket"]
    assert path not in rows


def test_the_alarm_handler_opens_no_ledger_path_option() -> None:
    text = (ROOT / ALARM_HANDLER).read_text(encoding="utf-8")
    assert "options.ledger" not in text
    assert "read_bytes" not in text
