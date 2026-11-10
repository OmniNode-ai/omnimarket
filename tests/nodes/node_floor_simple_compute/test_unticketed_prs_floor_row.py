# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The floor names the pull requests that cite no ticket as one row (OMN-23184).

The projection flags an open, non-draft pull request by a human author that cites no ticket; the floor reads those
items and draws one named row with the pull request list. No flagged pull request, no row.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
import yaml

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.floor_17a import (
    ModelFloorSimple,
    ModelFloorSimpleInput,
    ModelFloorSimpleOverlay,
)
from omnimarket.models.work_state_projection import (
    ModelPullRequestSource,
    ModelWorkScope,
    ModelWorkStateRequest,
    ModelWorkStateRules,
)
from omnimarket.nodes.node_floor_simple_compute.handlers.handler_floor_simple import (
    HandlerFloorSimple,
)
from omnimarket.nodes.node_floor_simple_effect.handlers.sources import (
    work_state_unticketed_prs,
)
from omnimarket.nodes.node_work_state_projection_compute.handlers.handler_work_state_projection import (
    HandlerWorkStateProjection,
)
from tests.nodes.node_factory_floor_compute.support import NOW
from tests.nodes.node_floor_simple_effect.support import overlay_text

pytestmark = pytest.mark.unit

EXAMPLE_OVERLAY = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_work_state_projection_compute/overlay.example.yaml"
)
RULES = load_structured_overlay(
    "node_work_state_projection_compute",
    ModelWorkStateRules,
    explicit=yaml.safe_load(EXAMPLE_OVERLAY.read_text(encoding="utf-8")),
)


def _pr(number: int, author: str, ticket: str | None = None) -> ModelPullRequestSource:
    return ModelPullRequestSource(
        repo="omnimarket",
        number=number,
        title="t",
        ticket=ticket,
        state="OPEN",
        author=author,
    )


def _doc(prs: tuple[ModelPullRequestSource, ...]) -> ModelFloorSimple:
    projection = HandlerWorkStateProjection().handle(
        ModelWorkStateRequest(
            now=NOW, rules=RULES, scope=ModelWorkScope(), pull_requests=prs
        )
    )
    return HandlerFloorSimple().handle(
        ModelFloorSimpleInput(
            now=NOW,
            overlay=ModelFloorSimpleOverlay.model_validate(
                overlay_text("node_floor_simple_compute")
            ),
            open_prs=10,
            open_prs_before=12,
            merges_in_window=4,
            watcher_read_at=NOW - timedelta(minutes=1),
            slots_working=5,
            slots_total=20,
            record_read_at=NOW - timedelta(minutes=1),
            unticketed_prs=work_state_unticketed_prs(projection),
            lab=None,
            receipts=None,
            go_given_through=0,
        )
    )


def test_one_named_row_lists_the_prs_that_cite_no_ticket() -> None:
    doc = _doc(
        (
            _pr(1, "alice"),
            _pr(2, "alice", ticket="OMN-5"),
            _pr(3, "dependabot[bot]"),
            _pr(4, "bob"),
        ),
    )
    rows = [c for c in doc.causes if c.id == "prs:no-ticket"]
    assert len(rows) == 1
    assert rows[0].prs == ("omnimarket#1", "omnimarket#4")
    assert rows[0].size == 2
    assert rows[0].size_unit == "pull requests"
    assert "ticket" in rows[0].title.lower()


def test_no_flagged_pr_no_row() -> None:
    doc = _doc((_pr(2, "alice", ticket="OMN-5"), _pr(3, "dependabot[bot]")))
    assert all(c.id != "prs:no-ticket" for c in doc.causes)
