# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Retrospective cases from test_standup_facts.py, invoked locally."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_standup_retrospective_compute.handlers.handler_standup_retrospective import (
    HandlerStandupRetrospective,
    retrospective_lines,
)
from omnimarket.nodes.node_standup_retrospective_compute.models.model_standup_retrospective import (
    ModelStandupRetrospectiveRequest,
    ModelStandupRetrospectiveResult,
)

pytestmark = pytest.mark.unit


def run(until: str, path: Path | None) -> ModelStandupRetrospectiveResult:
    return HandlerStandupRetrospective().handle(
        ModelStandupRetrospectiveRequest(
            until=until, entries=path.read_text() if path is not None else None
        )
    )


FRIDAY_SINCE = "2026-10-01T17:50:00Z"
FRIDAY_UNTIL = "2026-10-02T17:50:00Z"
RETRO = [
    {
        "role": "platform engineer",
        "name": "Pat Example",
        "product_use": "delegated a test fix with /onex:delegate",
        "idea": "show the delegation receipt in the session footer",
        "process_change": "pair on the first delegated ticket each week",
    },
    {
        "role": "product designer",
        "who": "Sam Sample",
        "product_use": "read the beta board each morning",
        "friction": "the board hides which criterion cleared",
        "process_change": "",
    },
    {"role": "operator", "product_use": "ran the merge drain"},
]


def retro_file(tmp_path: Path, entries: Any = RETRO) -> Path:
    path = tmp_path / "retro.json"
    path.write_text(json.dumps(entries))
    return path


def test_friday_retrospective_section_per_person(tmp_path: Path) -> None:
    """OMN-21286 AC1: a Friday run emits use, one idea or friction, one process change per person."""
    facts = run(FRIDAY_UNTIL, retro_file(tmp_path))
    retro = facts.retrospective
    assert retro["friday"] is True
    people = {row["role"]: row for row in retro["people"]}
    assert list(people) == ["platform engineer", "product designer", "operator"]
    assert people["platform engineer"] == {
        "role": "platform engineer",
        "product_use": "delegated a test fix with /onex:delegate",
        "idea_or_friction": "show the delegation receipt in the session footer",
        "kind": "idea",
        "process_change": "pair on the first delegated ticket each week",
        "missing": [],
    }
    assert people["product designer"]["kind"] == "friction"
    assert people["product designer"]["missing"] == ["process_change"]
    assert people["operator"]["missing"] == ["idea_or_friction", "process_change"]


def test_retrospective_absent_on_other_days(tmp_path: Path) -> None:
    """A run whose window ends on any other day builds no retrospective and no drafts."""
    facts = run("2026-10-01T17:50:00Z", retro_file(tmp_path))
    assert facts.retrospective == {}
    assert facts.ticket_drafts == []
    assert any("not a Friday" in note for note in facts.notes)


def test_friday_without_input_is_an_empty_section_and_a_note() -> None:
    facts = run(FRIDAY_UNTIL, None)
    assert facts.retrospective == {"friday": True, "people": []}
    assert facts.ticket_drafts == []
    assert any("--retro" in note for note in facts.notes)


@pytest.mark.parametrize(
    "entries",
    [{"role": "x"}, [{"product_use": "no role"}], [{"role": ""}], "not json"],
)
def test_unreadable_retrospective_entries_are_errors(
    tmp_path: Path, entries: Any
) -> None:
    path = tmp_path / "retro.json"
    path.write_text(entries if isinstance(entries, str) else json.dumps(entries))
    facts = run(FRIDAY_UNTIL, path)
    assert facts.retrospective["people"] == []
    assert any(error.startswith("retrospective:") for error in facts.errors)


def test_each_idea_is_one_backlog_draft_credited_by_role(tmp_path: Path) -> None:
    """OMN-21286 AC2: one Backlog draft per contributed idea, credited by role, no person name."""
    facts = run(FRIDAY_UNTIL, retro_file(tmp_path))
    drafts = facts.ticket_drafts
    assert [draft["credit"] for draft in drafts] == [
        "platform engineer",
        "product designer",
    ]
    assert all(draft["state"] == "Backlog" for draft in drafts)
    assert drafts[0]["title"] == "show the delegation receipt in the session footer"
    assert drafts[1]["kind"] == "friction"
    assert "credited to the platform engineer" in drafts[0]["description"]
    text = json.dumps(drafts)
    for name in ("Pat Example", "Sam Sample", "Pat", "Sam"):
        assert name not in text
    assert all(draft["filed"] is False for draft in drafts)


def test_retrospective_lines_renders_roles_and_unfiled_drafts(tmp_path: Path) -> None:
    facts = run(FRIDAY_UNTIL, retro_file(tmp_path))
    output = "\n".join(retrospective_lines(facts.retrospective, facts.ticket_drafts))
    assert "Pat Example" not in output
    assert "Sam Sample" not in output
    assert "retrospective (3):" in output
    assert "  platform engineer: use: delegated a test fix" in output
    assert "ticket drafts (2, not filed):" in output
    assert (
        "  [Backlog] show the delegation receipt in the session footer (credit: platform engineer)"
        in output
    )
    assert (
        "operator: use: ran the merge drain; idea or friction: not reported; process change: not reported"
        in output
    )
    assert retrospective_lines({}, []) == []


def test_unparsable_text_is_a_retrospective_error() -> None:
    facts = HandlerStandupRetrospective().handle(
        ModelStandupRetrospectiveRequest(until=FRIDAY_UNTIL, entries="{not json")
    )
    assert facts.errors
    assert facts.errors[0].startswith("retrospective:")
