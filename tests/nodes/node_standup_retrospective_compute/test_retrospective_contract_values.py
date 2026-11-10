# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21286: the retrospective's values come from the contract or its overlay, never the handler.

AC1 (a Friday run emits the section) and AC2 (one role-credited Backlog draft per idea, no person
name) hold for the packaged defaults; these tests hold that each value that shaped them is the
contract's, that an overlay moves it, and that a credit can only be a role of the vocabulary.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_standup_retrospective_compute import (
    NodeStandupRetrospectiveCompute,
)
from omnimarket.nodes.node_standup_retrospective_compute.models.model_standup_retrospective import (
    ModelStandupRetrospectiveRequest,
)
from tests.node_overlay_support import install_node_overlay

pytestmark = pytest.mark.unit

FRIDAY = "2026-10-02T17:50:00Z"
THURSDAY = "2026-10-01T17:50:00Z"
ENTRIES = [
    {
        "role": "platform engineer",
        "product_use": "delegated a test fix",
        "idea": "show the delegation receipt in the footer",
        "process_change": "pair on the first delegated ticket",
    }
]


def run(until: str, entries: Any = ENTRIES) -> Any:
    return NodeStandupRetrospectiveCompute().handle(
        ModelStandupRetrospectiveRequest(until=until, entries=json.dumps(entries))
    )


def test_the_retrospective_weekday_is_the_overlay_value(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Moving the weekday in the overlay moves the section; the handler holds no Friday."""
    assert run(FRIDAY).retrospective["friday"] is True
    assert run(THURSDAY).retrospective == {}
    install_node_overlay(
        monkeypatch, tmp_path, "standup_retrospective", {"retro_weekday": 3}
    )
    assert run(THURSDAY).retrospective["friday"] is True
    assert run(FRIDAY).retrospective == {}


def test_the_draft_state_and_credit_grammar_are_the_overlay_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install_node_overlay(
        monkeypatch,
        tmp_path,
        "standup_retrospective",
        {"draft_state": "Triage", "credit_template": "role: {role}"},
    )
    (draft,) = run(FRIDAY).ticket_drafts
    assert draft["state"] == "Triage"
    assert draft["credit"] == "role: platform engineer"


def test_the_section_text_is_the_overlay_value(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install_node_overlay(
        monkeypatch,
        tmp_path,
        "standup_retrospective",
        {"section_heading": "weekly review ({count}):"},
    )
    assert run(FRIDAY).section_lines[0] == "weekly review (1):"


def test_a_credit_outside_the_role_vocabulary_is_refused_and_never_drafted() -> None:
    """AC2: a person's name in the role cell would reach the draft as the credit; the vocabulary refuses it."""
    result = run(
        FRIDAY, [{"role": "Pat Example", "idea": "an idea", "product_use": "used it"}]
    )
    assert result.ticket_drafts == []
    assert result.retrospective["people"] == []
    assert any(
        error.startswith("retrospective:") and "role" in error
        for error in result.errors
    )
    assert "Pat Example" not in json.dumps(result.model_dump())


def test_the_role_vocabulary_is_the_overlay_value(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    entries = [{"role": "support lead", "idea": "an idea"}]
    assert run(FRIDAY, entries).ticket_drafts == []
    install_node_overlay(
        monkeypatch,
        tmp_path,
        "standup_retrospective",
        {"role_vocabulary": ["support lead"]},
    )
    assert [d["credit"] for d in run(FRIDAY, entries).ticket_drafts] == ["support lead"]
