# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelBacklogItem (SM-00): a prose engine is counted and skipped, never dispatched (OMN-21475, T-S3)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.handlers.handler_engine_ladder import load_engine_ladder
from omnimarket.models.model_engine_ladder import CONTEXT_KEY
from omnimarket.nodes.node_idle_slot_fill_effect import models, overlay
from omnimarket.nodes.node_idle_slot_fill_effect.handlers import (
    handler_idle_slot_fill as handler,
)
from omnimarket.nodes.node_idle_slot_fill_effect.models.model_idle_slot_fill import (
    REPO_KINDS_CONTEXT_KEY,
    ModelBacklogItem,
    backlog_item_refusal,
)
from tests.nodes.node_idle_slot_fill_effect.overlay_fixture import (
    IDLE_SLOT_FILL,
    write_overlay,
)

pytestmark = pytest.mark.unit

NOW = "2026-10-18T21:30:00Z"
LINES = ["MISSING lab-headroom:host-a | free Claude slots 3 | FIX: dispatch"]
HOSTS = [
    {
        "name": "host-a",
        "local": False,
        "admission": {"claude_slots": 2, "codex_slots": 0, "probe_error": None},
    }
]


def item(key: str, **fields: Any) -> dict[str, Any]:
    return {
        "kind": "ticket",
        "key": key,
        "repo": "omnimarket",
        "ticket": "OMN-21475",
        **fields,
    }


def fill(
    *backlog: dict[str, Any], repo_required_kinds: frozenset[str] = frozenset()
) -> models.ModelIdleSlotFillResult:
    return handler.HandlerIdleSlotFill().handle(
        models.ModelIdleSlotFillRequest(
            now=NOW,
            hosts=HOSTS,
            lines=LINES,
            backlog=list(backlog),
            standing_tickets={"ticket": "OMN-9101"},
            repo_required_kinds=repo_required_kinds,
        )
    )


def fixture_contract(tmp_path: Path, extra_rung: str | None) -> Path:
    entries = [
        {"rung": e.rung, "host_engine": e.host_engine}
        for e in load_engine_ladder().entries
    ]
    if extra_rung:
        entries.append({"rung": extra_rung, "host_engine": "claude_opus"})
    path = tmp_path / "contract.yaml"
    path.write_text(yaml.safe_dump({"config": {"engine_ladder": entries}}))
    return path


# AC1 ------------------------------------------------------------------------------------


def test_a_prose_engine_is_skipped_and_counted_and_no_lane_is_chosen() -> None:
    result = fill(item("prose", required_engine="whatever is cheapest right now"))
    assert result.backlog_mismatched == 1
    assert len(result.skipped) == 1
    assert result.skipped[0]["key"] == "prose"
    assert result.skipped[0]["why"].startswith("backlog-item-invalid: required_engine")
    assert result.sends == []


def test_the_model_refuses_free_text_and_a_non_text_engine() -> None:
    for bad in ("use opus if hard", 7):
        with pytest.raises(ValidationError, match="required_engine"):
            ModelBacklogItem.model_validate(item("x", required_engine=bad))


def test_the_prose_item_does_not_stop_the_good_item_beside_it() -> None:
    result = fill(
        item("prose", required_engine="a guess"),
        item("good", required_engine="claude"),
    )
    assert result.backlog_mismatched == 1
    assert [s["key"] for s in result.sends] == ["good"]


@pytest.mark.parametrize("empty", [None, "", "   "])
def test_no_engine_requirement_is_a_valid_item(empty: str | None) -> None:
    model = ModelBacklogItem.model_validate(item("x", required_engine=empty))
    assert model.required_engine is None


# AC2 ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "engine", ["claude", "claude-sonnet", "Claude_Sonnet", "sonnet", "codex", "opus"]
)
def test_families_aliases_and_models_are_valid_engines(engine: str) -> None:
    assert backlog_item_refusal(item("x", required_engine=engine)) is None


def test_the_family_claude_resolves_through_the_policy_to_sonnet() -> None:
    result = fill(item("fam", required_engine="claude"))
    assert result.backlog_mismatched == 0
    assert [(s["key"], s["model"]) for s in result.sends] == [("fam", "sonnet")]


# AC5: the ladder is the contract's -----------------------------------------------------


def test_a_fixture_contract_with_a_new_rung_makes_an_item_requiring_it_valid(
    tmp_path: Path,
) -> None:
    raw = item("future", required_engine="claude-opus-9")
    assert backlog_item_refusal(raw) is not None
    ladder = load_engine_ladder(fixture_contract(tmp_path, "claude-opus-9"))
    assert "claude-opus-9" in ladder.rungs
    assert backlog_item_refusal(raw, context={CONTEXT_KEY: ladder}) is None


def test_a_contract_without_the_ladder_refuses_naming_the_key(tmp_path: Path) -> None:
    path = tmp_path / "contract.yaml"
    path.write_text(yaml.safe_dump({"config": {}}))
    with pytest.raises(ValueError, match=r"config\.engine_ladder"):
        load_engine_ladder(path)


# The repository, supplied by the overlay -----------------------------------------------


def test_a_kind_the_context_lists_needs_a_repository() -> None:
    context = {REPO_KINDS_CONTEXT_KEY: ("ticket",)}
    assert backlog_item_refusal(item("x", repo=""), context=context) is not None
    assert backlog_item_refusal(item("x", repo=None), context=context) is not None
    assert backlog_item_refusal(item("x"), context=context) is None
    assert backlog_item_refusal(item("x", repo="")) is None


def test_the_overlay_decides_which_kind_needs_a_repository(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env = write_overlay(
        tmp_path, idle_slot_fill={**IDLE_SLOT_FILL, "repo_required_kinds": ["ticket"]}
    )
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", env["ONEX_SKILL_OVERLAY_ROOTS"])
    kinds = overlay.repo_required_kinds()
    assert kinds == frozenset({"ticket"})
    result = fill(item("norepo", repo=""), repo_required_kinds=kinds)
    assert result.backlog_mismatched == 1
    assert "repo is required for kind 'ticket'" in result.skipped[0]["why"]
    assert result.sends == []
