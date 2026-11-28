# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The per-ticket selection rules are overlay data, not code literals (OMN-26762)."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.models.lab_fill.model_lab_fill_selection_overlay import (
    ModelLabFillSelectionOverlay,
)
from omnimarket.nodes.node_lab_fill_selection_compute.handlers.handler_lab_fill_selection import (
    HandlerLabFillSelection,
)
from omnimarket.nodes.node_lab_fill_selection_compute.models import (
    EnumLabFillSkipReason,
    ModelLabFillCandidateInput,
    ModelLabFillDeployment,
    ModelLabFillSelectionRequest,
)

BASE = {
    "companion_repositories": [],
    "not_work_repositories": [],
    "document_repositories": [],
}
RULES = {
    "cooldown_hours": 6,
    "attempt_limit": 2,
    "attempt_window_hours": 24,
    "repeat_limit": 2,
    "one_worker_per_ticket": True,
    "skip_done_gate_proven": True,
    "host_rotation": ["h1", "h2"],
}
NOW = "2026-11-27T22:00:00Z"


def _deployment(**rules: object) -> ModelLabFillDeployment:
    return ModelLabFillSelectionOverlay.model_validate(
        {**BASE, "selection_rules": {**RULES, **rules}}
    ).deployment()


def _cand(ticket: str, **kw: object) -> ModelLabFillCandidateInput:
    return ModelLabFillCandidateInput(
        key=kw.pop("key", ticket),
        kind="ticket",
        ticket=ticket,
        **kw,  # type: ignore[arg-type]
    )


def _run(deployment: ModelLabFillDeployment, *cands: ModelLabFillCandidateInput):
    return HandlerLabFillSelection().handle(
        ModelLabFillSelectionRequest(
            now=NOW, candidates=cands, ledger_lines=(), deployment=deployment
        )
    )


def test_lab_fill_selection_rules_overlay_loads_as_data() -> None:
    rules = _deployment().selection_rules
    assert rules is not None
    assert rules.cooldown_hours == 6
    assert rules.host_rotation == ("h1", "h2")


def test_lab_fill_selection_rules_overlay_rule_keys_are_required_and_closed() -> None:
    with pytest.raises(ValueError, match="selection_rules"):
        ModelLabFillSelectionOverlay.model_validate(
            {**BASE, "selection_rules": {"cooldown_hours": 6}}
        )
    with pytest.raises(ValueError, match="selection_rules"):
        ModelLabFillSelectionOverlay.model_validate(
            {**BASE, "selection_rules": {**RULES, "surprise": 1}}
        )


def test_lab_fill_selection_rules_overlay_placed_within_cooldown_skips_and_window_is_data() -> (
    None
):
    placed = _cand("OMN-1", last_placed_at="2026-11-27T20:00:00Z")
    held = _run(_deployment(cooldown_hours=6), placed).decisions[0]
    assert held.reason is EnumLabFillSkipReason.PLACED_WITHIN_COOLDOWN
    free = _run(_deployment(cooldown_hours=1), placed).decisions[0]
    assert free.reason is None


def test_lab_fill_selection_rules_overlay_live_worker_skips() -> None:
    d = _run(_deployment(), _cand("OMN-1", claim_holder="lane-x")).decisions[0]
    assert d.reason is EnumLabFillSkipReason.OWNED


def test_lab_fill_selection_rules_overlay_done_gate_proven_skips_only_when_declared() -> (
    None
):
    proven = _cand("OMN-1", done_gate="proven")
    assert (
        _run(_deployment(), proven).decisions[0].reason
        is EnumLabFillSkipReason.DONE_GATE_PROVEN
    )
    assert (
        _run(_deployment(skip_done_gate_proven=False), proven).decisions[0].reason
        is None
    )


def test_lab_fill_selection_rules_overlay_one_worker_per_ticket() -> None:
    a, b = _cand("OMN-1", key="a"), _cand("OMN-1", key="b")
    two = _run(_deployment(), a, b).decisions
    assert [d.reason for d in two] == [
        None,
        EnumLabFillSkipReason.ONE_WORKER_PER_TICKET,
    ]
    both = _run(_deployment(one_worker_per_ticket=False), a, b).decisions
    assert [d.reason for d in both] == [None, None]


def test_lab_fill_selection_rules_overlay_host_rotation_cycles_declared_hosts() -> None:
    result = _run(_deployment(), _cand("OMN-1"), _cand("OMN-2"), _cand("OMN-3"))
    assert dict(result.hosts) == {"OMN-1": "h1", "OMN-2": "h2", "OMN-3": "h1"}
    none = _run(_deployment(host_rotation=[]), _cand("OMN-1"))
    assert dict(none.hosts) == {}


def test_lab_fill_selection_rules_overlay_no_rules_block_keeps_prior_behaviour() -> (
    None
):
    dep = ModelLabFillSelectionOverlay.model_validate(BASE).deployment()
    assert dep.selection_rules is None
    assert _run(dep, _cand("OMN-1")).decisions[0].reason is None


def test_lab_fill_selection_rules_overlay_no_rule_literals_in_handler() -> None:
    src = Path(
        "src/omnimarket/nodes/node_lab_fill_selection_compute/handlers/handler_lab_fill_selection.py"
    ).read_text(encoding="utf-8")
    assert "cooldown_hours=" not in src
