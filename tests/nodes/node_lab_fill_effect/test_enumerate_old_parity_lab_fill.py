# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The enumerate node answers what the old enumerate answered, for the same inputs (OMN-23374).

fixtures/old_enumerate_recorded.json holds the old enumerate's own decisions, recorded by running its
source lines unchanged (the row validation, the skip order, the attachment readers) over rows of the
declared approved-work file, each mutated to break one rule, and over every combination of the facts
that decide a skip. These tests replay the same inputs through the node and compare. The failing
control is inside each test: a node that drops one rule or reorders the skip chain disagrees on at
least one recorded case, named in the assertion.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from omnimarket.nodes.node_lab_fill_effect.handlers import HandlerLabFillApprovedWork
from omnimarket.nodes.node_lab_fill_effect.handlers.handler_lab_fill_approved_work import (
    row_is_valid,
    skip_reason,
)
from omnimarket.nodes.node_lab_fill_effect.handlers.helpers_lab_fill_enumerate import (
    attached_prs,
    merged_implementations,
)
from omnimarket.nodes.node_lab_fill_effect.models import (
    ModelLabFillApprovedWorkRequest,
)
from omnimarket.nodes.node_lab_fill_effect.protocols import (
    LabFillApprovedTicket,
    LabFillMergedHistory,
    LabFillPrFacts,
)

from .fakes import FakeClock

RECORDED = json.loads(
    (Path(__file__).parent / "fixtures" / "old_enumerate_recorded.json").read_text(
        encoding="utf-8"
    )
)
KINDS = ("process-fix", "partial-node", "wiring")
BASES = ("linear-state", "acceptance-on-main", "merged-history", "duplicate")


def test_recorded_old_output_covers_every_skip_reason() -> None:
    """Positive control: the fixture is not trivially empty and holds both valid and invalid rows."""
    reasons = {case[-1] for case in RECORDED["skip"]}
    assert reasons >= {
        "",
        "done:pr-merged",
        "done:ticket",
        "done:declared",
        "unreadable:ticket",
        "unreadable:merged-history:omnimarket",
        "blocked-until:OMN-9",
        "owned:open-pr:omnimarket#7,repo_b#2",
    }
    assert any(c.startswith("stale:merged-after-check:omnimarket@") for c in reasons)
    flags = {v["invalid"] for v in RECORDED["validation"]}
    assert flags == {True, False}


def test_row_validation_matches_the_old_enumerate() -> None:
    ids: set[str] = set()
    disagree = []
    for case in RECORDED["validation"]:
        row = case["row"]
        valid = row_is_valid(row, ids, KINDS, BASES)
        if valid != (not case["invalid"]):
            disagree.append(row if not isinstance(row, dict) else row["id"])
        if valid:
            ids.add(row["id"])
    assert disagree == []


@pytest.mark.parametrize(
    "case", RECORDED["skip"], ids=lambda c: "|".join(str(x) for x in c)
)
def test_skip_order_matches_the_old_enumerate(case: list[object]) -> None:
    (
        node_state,
        merged,
        declared_done,
        unread,
        history,
        checked,
        until,
        open_pr,
        want,
    ) = case
    row: dict[str, object] = {"ticket": "OMN-1", "repo": "omnimarket"}
    if declared_done:
        row["done"] = True
    if checked:
        row["checked_at"] = checked
    if until:
        row["blocked_until"] = until
    node = (
        LabFillApprovedTicket(
            ticket="OMN-1",
            state_type=str(node_state),
            assignee_id="",
            updated_at="",
            description="",
            blockers=(),
            prs=(),
            merged_implementations=(),
        )
        if node_state
        else None
    )
    prs = LabFillPrFacts(
        frozenset({"OMN-1"}) if merged else frozenset(),
        {"OMN-1": ("omnimarket#7", "repo_b#2")} if open_pr else {},
    )
    hist = LabFillMergedHistory(
        {"OMN-1": ("omnimarket", "2026-10-10T00:00:00Z", "b" * 40)} if history else {},
        frozenset({"omnimarket"}) if unread else frozenset(),
    )
    assert skip_reason(row, node, prs, hist) == want


def test_attachment_readers_match_the_old_enumerate() -> None:
    disagree = []
    for case in RECORDED["attachments"]:
        atts = case["attachments"]
        got = (attached_prs(atts), merged_implementations(atts, case["children"]))
        if got != (tuple(case["attached"]), tuple(case["merged"])):
            disagree.append(case)
    assert disagree == []


def test_the_node_read_returns_the_rows_the_old_enumerate_kept() -> None:
    """Whole operation: the valid rows, and the seen and invalid counts, equal the recorded old read."""
    rows = [case["row"] for case in RECORDED["validation"]]
    old_kept = [c["row"]["id"] for c in RECORDED["validation"] if not c["invalid"]]
    old_invalid = sum(1 for c in RECORDED["validation"] if c["invalid"])
    clock = FakeClock()
    nodes = {
        str(r["ticket"]): LabFillApprovedTicket(
            ticket=str(r["ticket"]),
            state_type="started",
            assignee_id="",
            updated_at="2026-11-02T00:00:00Z",
            description="",
            blockers=(),
            prs=(),
            merged_implementations=(),
        )
        for r in rows
        if isinstance(r, dict) and isinstance(r.get("ticket"), str)
    }

    class Ports:
        def read_rows(self, path: str) -> list[object]:
            return list(rows)

        def read_prs(self) -> LabFillPrFacts:
            return LabFillPrFacts(frozenset(), {})

        def read_history(
            self, wanted: Sequence[str], budget_s: float
        ) -> LabFillMergedHistory:
            return LabFillMergedHistory({}, frozenset())

        def read_tickets(
            self, wanted: Sequence[str], budget_s: float
        ) -> Mapping[str, LabFillApprovedTicket]:
            return nodes

        def probe(self, ref: str, repo: str) -> bool:
            return False

    ports = Ports()
    handler = HandlerLabFillApprovedWork(
        rows=ports.read_rows,
        prs=ports.read_prs,
        history=ports.read_history,
        tickets=ports.read_tickets,
        acceptance=ports.probe,
        clock=clock,
    )
    result = handler.handle(
        ModelLabFillApprovedWorkRequest(
            path="approved.json",
            kinds=KINDS,
            done_bases=BASES,
            fleet_repos=("omnimarket",),
            max_rows=500,
            budget_s=60.0,
            stage_floor_s=10.0,
        )
    )
    assert result.state == "ok"
    assert [c.row["id"] for c in result.candidates] == old_kept
    assert sum(result.invalid.values()) == old_invalid
    assert sum(result.seen.values()) == len(rows)
