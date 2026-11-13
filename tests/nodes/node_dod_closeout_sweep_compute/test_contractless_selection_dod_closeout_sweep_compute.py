# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20655: the closer's selection does not fill its run with tickets that have no contract.

On live data the 2026-11-08 sweeps (runs 0651Z to 0345Z) selected 12 tickets a run and flipped
none; 9 of the 12 had no contracts/<id>.yaml on any default branch, so dod_verify answered
VERIFY_COULD_NOT_RUN for them, and the same contractless tickets came back every run. The
has_contract rule ranks them last, but a rank still fills every free slot: with only 3
verifiable tickets in scope the other 9 slots went to contractless ones. These cases pin the
rule that follows: a contractless ticket is selected only through the reserve.
Golden chain: the scope_bundle decision through the handler. Error chain: the overlay that
drops the setting is refused, and the named injected break (the setting off) fills the run.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import omnimarket.nodes.node_dod_closeout_sweep_compute as node_package
from omnimarket.events.closeout_scope_overlay import (
    ModelCloseoutScopeOverlay,
)
from omnimarket.events.dod_closeout_sweep import (
    ModelDodCloseoutDecisionRequest,
)
from omnimarket.nodes.node_dod_closeout_sweep_compute.handlers.closer_scope_overlay import (
    load_scope_overlay,
)
from omnimarket.nodes.node_dod_closeout_sweep_compute.handlers.closer_scope_rules import (
    bundle_text,
)
from omnimarket.nodes.node_dod_closeout_sweep_compute.handlers.handler_dod_closeout_sweep import (
    HandlerDodCloseoutSweep,
)

pytestmark = pytest.mark.unit

NODE_DIR = Path(node_package.__file__).parent
LIMIT = 12
# The shape of the 2026-11-08 scope: 3 tickets can reach a verdict, the rest have no contract.
WITH_CONTRACT = {"OMN-20", "OMN-21", "OMN-22"}
# Ties on the closeout comment fall to the ticket id as text, so the two reserved are these.
RESERVED = ["OMN-1", "OMN-10"]


def _overlay(**changes: Any) -> ModelCloseoutScopeOverlay:
    return load_scope_overlay().model_copy(update=changes)


def _scope(
    n: int, contracts: set[str], waiting: dict[str, list[str]] | None = None
) -> tuple[list[Any], list[Any], list[Any], list[Any]]:
    ids = [f"OMN-{i}" for i in range(1, n + 1)]
    cands = [
        {
            "id": i,
            "state": "In Review",
            "sources": ["started"],
            "last_closeout_comment_at": "2026-10-01T00:00:00.000Z",
        }
        for i in ids
    ]
    prs = [{"id": i, "merged": ["omnimarket#1"], "open": [], "error": ""} for i in ids]
    texts = [{"id": i, "waiting_on": w} for i, w in (waiting or {}).items()]
    state = [{"id": i, "has_contract": i in contracts} for i in ids]
    return cands, prs, texts, state


def _bundle(
    overlay: ModelCloseoutScopeOverlay,
    contracts: set[str] = WITH_CONTRACT,
    waiting: dict[str, list[str]] | None = None,
    n: int = 30,
) -> dict[str, Any]:
    cands, prs, texts, state = _scope(n, contracts, waiting)
    return json.loads(bundle_text(cands, prs, texts, state, LIMIT, overlay))


def _ids(bundle: dict[str, Any]) -> list[str]:
    return [c["id"] for c in bundle["candidates"]]


def test_the_overlay_declares_that_contractless_tickets_come_only_through_the_reserve() -> (
    None
):
    overlay = load_scope_overlay()
    assert overlay.no_contract_only_by_reserve is True
    assert overlay.no_contract_reserve == 2


def test_contractless_tickets_take_only_the_reserve_slots() -> None:
    """The live shape: 3 verifiable tickets in a scope of 30. The run holds those 3 and the 2
    reserved oldest contractless ones, not 3 and 9."""
    bundle = _bundle(load_scope_overlay())
    chosen = _ids(bundle)
    assert set(chosen) >= WITH_CONTRACT
    assert [i for i in chosen if i not in WITH_CONTRACT] == RESERVED
    assert len(chosen) == 5
    assert bundle["totals"]["selected"] == 5
    assert bundle["no_contract"] == RESERVED


def test_failing_control_with_the_setting_off_the_contractless_fill_the_run() -> None:
    """Named injected break: the overlay without the setting is the selection that ran on
    2026-11-08, 9 of 12 slots on tickets dod_verify cannot run for."""
    broken = _overlay(no_contract_only_by_reserve=False)
    chosen = _ids(_bundle(broken))
    assert len(chosen) == LIMIT
    assert len([i for i in chosen if i not in WITH_CONTRACT]) == 9


def test_verifiable_tickets_fill_the_run_before_the_reserve() -> None:
    contracts = {f"OMN-{i}" for i in range(10, 31)}  # 21 tickets with a contract
    chosen = _ids(_bundle(load_scope_overlay(), contracts))
    assert len(chosen) == LIMIT
    assert [i for i in chosen if i not in contracts] == ["OMN-1", "OMN-2"]


def test_a_scope_with_no_verifiable_ticket_keeps_only_the_reserve() -> None:
    chosen = _ids(_bundle(load_scope_overlay(), set()))
    assert chosen == RESERVED


def test_reserve_zero_selects_no_contractless_ticket() -> None:
    chosen = _ids(_bundle(_overlay(no_contract_reserve=0)))
    assert set(chosen) == WITH_CONTRACT


def test_a_ticket_waiting_on_a_post_merge_condition_is_still_rechecked() -> None:
    """waiting_on is the first rank rule and its tickets were adjudicated before; a contractless
    one that records a condition is not cut by the new rule."""
    chosen = _ids(_bundle(load_scope_overlay(), waiting={"OMN-7": ["release cut"]}))
    assert chosen[0] == "OMN-7"
    assert set(chosen) >= WITH_CONTRACT


def test_a_scope_with_no_contract_reading_is_not_narrowed() -> None:
    cands, prs, texts, _ = _scope(30, WITH_CONTRACT)
    text = bundle_text(cands, prs, texts, None, LIMIT, load_scope_overlay())
    assert len(json.loads(text)["candidates"]) == LIMIT


def test_the_decision_returns_the_narrowed_selection() -> None:
    cands, prs, texts, state = _scope(30, WITH_CONTRACT)
    request = ModelDodCloseoutDecisionRequest.model_validate(
        {
            "kind": "scope_bundle",
            "date": "2026-11-09",
            "max_candidates": LIMIT,
            "scope_candidates": cands,
            "scope_pr_state": prs,
            "scope_text_state": texts,
            "scope_contract_state": state,
        }
    )
    result = HandlerDodCloseoutSweep().handle(request)
    assert result.scope_bundle is not None
    assert result.scope_bundle["totals"]["selected"] == 5


def test_an_overlay_that_drops_the_setting_is_refused_never_defaulted(
    tmp_path: Path,
) -> None:
    text = (NODE_DIR / "overlay.yaml").read_text(encoding="utf-8")
    broken = tmp_path / "overlay.yaml"
    broken.write_text(
        text.replace("no_contract_only_by_reserve: true\n", ""), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="no_contract_only_by_reserve"):
        load_scope_overlay(broken)
