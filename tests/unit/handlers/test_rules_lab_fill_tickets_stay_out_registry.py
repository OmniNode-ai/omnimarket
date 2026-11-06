# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-22017: the lab-fill stay-out sets are registry keys carrying their ruling ids.

The rule spells no stay-out set of its own. A fixture registry (the ``idle_slot_fill``
overlay) supplies them, so changing the registry changes the dropped candidates with no
code change, and each drop names the ruling that keeps it out.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.handlers.rules_lab_fill_tickets import plan_ticket_backlog
from omnimarket.models.lab_fill import (
    EnumLabFillTicketDrop,
    ModelLabFillStayOut,
    ModelLabFillTicketFacts,
)
from omnimarket.nodes.node_idle_slot_fill_effect import overlay
from tests.nodes.node_idle_slot_fill_effect.overlay_fixture import (
    IDLE_SLOT_FILL,
    write_overlay,
)

pytestmark = pytest.mark.unit

SPRINT = "Sprint 2026-10-26 → 2026-11-01: M4 End-to-End Part 4"
RULING_A = "RULING 2026-01-02T03:04:05Z lane=example-a"
RULING_B = "RULING 2026-02-03T04:05:06Z lane=example-b"

ISSUES = [
    {"id": "OMN-1", "title": "Build the widget panel", "repo": "repo-one"},
    {"id": "OMN-2", "title": "Tidy the parser", "repo": "repo-two"},
    {"id": "OMN-3", "title": "Mention repo-two in a doc", "repo": "repo-three"},
    {"id": "OMN-4", "title": "Plain work", "repo": "repo-three"},
    {"id": "OMN-5", "title": "Carrier work", "repo": "repo-three"},
]


def _plan(stay_out: tuple[ModelLabFillStayOut, ...]) -> dict[str, str]:
    facts = ModelLabFillTicketFacts.model_validate(
        {
            "today": "2026-10-27",
            "issues": [
                {
                    "status": "Todo",
                    "project": SPRINT,
                    "description": "- [ ] AC1: x",
                    **i,
                }
                for i in ISSUES
            ],
            "m4_carriers": {"C9": ("OMN-5",)},
            "stay_out": stay_out,
        }
    )
    plan = plan_ticket_backlog(facts)
    return {
        drop.id: drop.detail
        for drop in plan.drops
        if drop.reason is EnumLabFillTicketDrop.STAY_OUT
    }


def _registry(tmp_path: Path, stay_out: list[dict[str, Any]]) -> dict[str, str]:
    return write_overlay(
        tmp_path, idle_slot_fill={**IDLE_SLOT_FILL, "lab_fill_stay_out": stay_out}
    )


def test_changing_the_registry_stay_out_set_changes_the_drops(tmp_path: Path) -> None:
    first = overlay.stay_out(
        _registry(
            tmp_path / "a",
            [{"name": "widgets", "ruling": RULING_A, "terms": ["widget"]}],
        )
    )
    second = overlay.stay_out(
        _registry(
            tmp_path / "b",
            [
                {"name": "repo-two", "ruling": RULING_B, "repos": ["repo-two"]},
                {"name": "carrier", "ruling": RULING_A, "criteria": ["C9"]},
                {"name": "ticket", "ruling": RULING_A, "tickets": ["OMN-4"]},
            ],
        )
    )
    assert _plan(first) == {"OMN-1": f"widgets: {RULING_A}"}
    assert _plan(second) == {
        "OMN-2": f"repo-two: {RULING_B}",
        "OMN-3": f"repo-two: {RULING_B}",
        "OMN-4": f"ticket: {RULING_A}",
        "OMN-5": f"carrier: {RULING_A}",
    }


def test_an_empty_registry_set_drops_nothing_for_stay_out() -> None:
    assert _plan(()) == {}


def test_a_stay_out_set_without_a_ruling_id_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        ModelLabFillStayOut.model_validate({"name": "x", "ruling": "no ruling"})
    env = _registry(tmp_path, [{"name": "x", "ruling": "", "terms": ["x"]}])
    with pytest.raises(NodeOverlayError, match=r"lab_fill_stay_out\.0\.ruling"):
        overlay.stay_out(env)


def test_the_overlay_file_round_trips_the_ruling_id(tmp_path: Path) -> None:
    env = _registry(tmp_path, [{"name": "n", "ruling": RULING_B, "repos": ["r"]}])
    path = Path(env["ONEX_SKILL_OVERLAY_ROOTS"]) / "idle_slot_fill" / "overlay.yaml"
    assert (
        yaml.safe_load(path.read_text())["lab_fill_stay_out"][0]["ruling"] == RULING_B
    )
    assert overlay.stay_out(env)[0].ruling == RULING_B
