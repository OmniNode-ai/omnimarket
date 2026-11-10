# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The served floor lists delegation:idle from the work-state projection and the overlay's epics and owners (OMN-23313).

The whole cycle runs: a projection file with an operator-owned ticket under a delegation epic and no lane on it is read from
the registry home, and the simple floor written by the tick carries a red ``delegation:idle`` row whose owners resolve from
the overlay. The controls are the same home with the overlay block taken away (no row) and with a projection whose tickets
carry no assignee (the row's input is unread, which is a cause of its own and never an empty row).
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from omnimarket.handlers.emission_sink import InMemoryEmissionSink
from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.floor_17a import ModelFloorSimple, ModelFloorSimpleTickRequest
from omnimarket.models.work_state_projection import (
    ModelTicketSource,
    ModelWorkScope,
    ModelWorkStateRequest,
    ModelWorkStateRules,
)
from omnimarket.nodes.node_floor_simple_effect.handlers.handler_floor_simple_tick import (
    HandlerFloorSimpleTick,
)
from omnimarket.nodes.node_work_state_projection_compute.handlers.handler_work_state_projection import (
    HandlerWorkStateProjection,
)
from tests.nodes.node_floor_simple_effect.support import (
    NOW,
    OVERLAYS,
    build_home,
    environ,
    put_projection,
)

pytestmark = pytest.mark.unit

EXAMPLE = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_work_state_projection_compute/overlay.example.yaml"
)
RULES = load_structured_overlay(
    "node_work_state_projection_compute",
    ModelWorkStateRules,
    explicit=yaml.safe_load(EXAMPLE.read_text(encoding="utf-8")),
)
BLOCK = {
    "epics": ["EPIC-1"],
    "operator_assignees": ["me"],
    "title": "Operator-owned delegation tickets can start and have no lane",
}


def overlay_root(tmp_path: Path, *, block: bool) -> Path:
    root = tmp_path / "overlays"
    shutil.copytree(OVERLAYS, root)
    path = root / "node_floor_simple_compute" / "overlay.yaml"
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    if block:
        doc["causes"]["delegation_idle"] = BLOCK
        doc["causes"]["owners"]["delegation:idle"] = ["OMN-23313"]
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return root


def tick(tmp_path: Path, *, block: bool, assignee: str | None) -> ModelFloorSimple:
    home = build_home(tmp_path)
    projection = HandlerWorkStateProjection().handle(
        ModelWorkStateRequest(
            now=datetime(2026, 11, 5, 15, 0, tzinfo=UTC),
            rules=RULES,
            scope=ModelWorkScope(in_scope_tickets=("EPIC-1",)),
            tickets=(
                ModelTicketSource(ticket_id="EPIC-1", state_type="started"),
                ModelTicketSource(
                    ticket_id="D-1",
                    state_type="backlog",
                    parent="EPIC-1",
                    assignee=assignee,
                ),
            ),
        )
    )
    put_projection(home, "work_state", projection.model_dump(mode="json"))
    env = {
        **environ(home),
        "ONEX_SKILL_OVERLAY_ROOTS": str(overlay_root(tmp_path, block=block)),
    }
    result = HandlerFloorSimpleTick(sink=InMemoryEmissionSink(), clock=lambda: NOW).run(
        ModelFloorSimpleTickRequest(home=str(home)), environ=env
    )
    assert result.ok
    return ModelFloorSimple.model_validate_json(
        (home / ".claude_scratch/factory-floor/floor-simple.json").read_text(
            encoding="utf-8"
        )
    )


def test_golden_delegation_idle_board_lists_the_row_red_with_overlay_owners(
    tmp_path: Path,
) -> None:
    doc = tick(tmp_path, block=True, assignee="me")
    (cause,) = [c for c in doc.causes if c.id == "delegation:idle"]
    assert cause.state == "red"
    assert cause.tickets == ("D-1", "OMN-23313")
    assert cause.owners == ("D-1", "OMN-23313")
    assert doc.sources_read["delegation_tickets"] is True


def test_error_delegation_idle_board_without_the_overlay_block_has_no_row(
    tmp_path: Path,
) -> None:
    doc = tick(tmp_path, block=False, assignee="me")
    assert "delegation:idle" not in {c.id for c in doc.causes}
    assert "delegation_tickets" not in doc.sources_read


def test_error_delegation_idle_board_with_no_assignee_read_is_an_unread_cause(
    tmp_path: Path,
) -> None:
    doc = tick(tmp_path, block=True, assignee=None)
    ids = {c.id for c in doc.causes}
    assert "delegation:idle" not in ids
    assert "source:delegation_tickets" in ids
    assert doc.sources_read["delegation_tickets"] is False
