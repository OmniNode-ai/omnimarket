# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The seam from the verdict record to the check (OMN-24614): ledger STATUS rows in, the posted commit status out.

The rows are the ones the landing ledger facts node reads for the landing decision's own review gate, so one record feeds
both gates. A clean verdict row from an accepted reviewer lane for the exact head reads success; the same row from a lane
that is not an accepted reviewer, from another engine, or for an older head does not; an unreadable ledger posts nothing.
The check's reading equals the landing decision's reading of the same rows. Two injected breaks each change one cell the
seam depends on and the reading goes red.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from omnimarket.handlers.review_head_reading import read_head_review
from omnimarket.models.landing_ledger_row import ModelLandingLedgerRow
from omnimarket.models.model_landing_review import EnumReviewGateVerdict
from omnimarket.models.model_reading import ModelLedgerEvidenceRow
from omnimarket.nodes.node_pr_landing_ledger_facts_compute.handlers.handler_pr_landing_ledger_facts import (
    review_facts,
)
from omnimarket.nodes.node_reading_writer_effect.protocols import (
    ModelLedgerQuery,
    ModelLedgerRead,
)
from omnimarket.nodes.node_review_gate_context_orchestrator.adapters.inmemory_sources import (
    InMemoryOpenPullRequests,
)
from omnimarket.nodes.node_review_gate_context_orchestrator.adapters.ledger_review_verdicts import (
    LedgerReviewVerdicts,
)
from omnimarket.nodes.node_review_gate_context_orchestrator.models.model_review_gate_sweep import (
    ModelReviewGateSweepCommand,
    ModelReviewGateSweepResult,
)
from omnimarket.nodes.node_review_gate_context_orchestrator.overlay import (
    ModelReviewGateSweepSettings,
)
from tests.unit.nodes.node_review_gate_context_orchestrator.support import (
    HEAD,
    NOW,
    OLD_HEAD,
    Rig,
    subject,
)

pytestmark = pytest.mark.unit

SWEEP = ModelReviewGateSweepCommand(correlation_id="sweep-0001", post=True)
SETTINGS = ModelReviewGateSweepSettings(
    owner="example-org",
    repositories=("repo-a",),
    watcher_state_path_env="EXAMPLE_WATCHER_STATE_PATH",
    max_age_seconds=900,
    include_drafts=False,
    lookback_days=14,
    review_lane_prefixes=("orchestrator-",),
    review_engines=("fable",),
)


class _Rows:
    """An in-memory ledger row source over evidence rows; ``error`` makes every read fail."""

    def __init__(
        self, rows: tuple[ModelLedgerEvidenceRow, ...], error: str | None = None
    ) -> None:
        self._rows = rows
        self._error = error

    def read(self, query: ModelLedgerQuery) -> ModelLedgerRead:
        if self._error is not None:
            return ModelLedgerRead(error=self._error)
        return ModelLedgerRead(rows=self._rows)


def _row(
    *,
    head: str = HEAD,
    lane: str = "orchestrator-31d9",
    review: str = "fable",
    verdict: str = "CLEAN",
    pr: str = "repo-a#1",
) -> ModelLedgerEvidenceRow:
    stamp = datetime(2026, 11, 15, 12, 0, 0, tzinfo=UTC)
    cells = f"pr={pr} | head={head} | review={review} | verdict={verdict}"
    return ModelLedgerEvidenceRow(
        stamp=stamp,
        row_type="STATUS",
        lane=lane,
        cells={"pr": pr, "head": head, "review": review, "verdict": verdict},
        raw=f"2026-11-15T12:00:00Z | STATUS | lane={lane} | {cells} | review verdict",
    )


def _sweep(rows: _Rows, head: str = HEAD) -> tuple[Rig, ModelReviewGateSweepResult]:
    rig = Rig(
        InMemoryOpenPullRequests((subject("repo-a", 1, head),)),
        LedgerReviewVerdicts(rows, SETTINGS),
    )
    return rig, rig.handler.handle(SWEEP)


def test_a_clean_row_from_an_accepted_reviewer_for_the_head_reads_success() -> None:
    rig, result = _sweep(_Rows((_row(),)))
    assert result.entries[0].state.value == "success"
    assert result.entries[0].reason.value == "clean_on_head"
    assert rig.runs.posted[0]["state"] == "success"


@pytest.mark.parametrize(
    ("row", "reason"),
    [
        (_row(lane="ticket-9001"), "review_missing"),
        (_row(review="other-engine"), "review_missing"),
        (_row(head=OLD_HEAD), "review_superseded"),
        (_row(verdict="CHANGES_NEEDED"), "review_changes_needed"),
        (_row(head="abc"), "review_missing"),
    ],
)
def test_a_row_that_does_not_count_for_the_head_reads_failure(
    row: ModelLedgerEvidenceRow, reason: str
) -> None:
    rig, result = _sweep(_Rows((row,)))
    assert result.entries[0].state.value == "failure"
    assert result.entries[0].reason.value == reason
    assert rig.runs.posted[0]["state"] == "failure"


def test_an_unreadable_ledger_posts_nothing() -> None:
    rig, result = _sweep(_Rows((), error="ledger unreadable"))
    assert not result.ok
    assert result.unread == "ledger unreadable"
    assert rig.runs.posted == []


def test_the_check_reads_the_rows_the_way_the_landing_decision_does() -> None:
    rows = (_row(verdict="CHANGES_NEEDED"), _row())
    landing = review_facts(
        tuple(
            ModelLandingLedgerRow(
                ts="2026-11-15T12:00:00Z", rtype="STATUS", lane=r.lane, text=r.raw
            )
            for r in rows
        ),
        NOW,
        SETTINGS.review_lane_prefixes,
        SETTINGS.review_engines,
    )
    mine = LedgerReviewVerdicts(_Rows(rows), SETTINGS).review_facts(NOW)
    assert mine == landing
    assert (
        read_head_review(mine["repo-a#1"], HEAD).verdict is EnumReviewGateVerdict.PASS
    )


def test_break_a_renamed_head_cell_reads_the_verdict_as_missing() -> None:
    renamed = _row().model_copy(update={"raw": _row().raw.replace("head=", "sha=")})
    assert _sweep(_Rows((_row(),)))[1].entries[0].state.value == "success"
    assert _sweep(_Rows((renamed,)))[1].entries[0].state.value == "failure"


def test_break_a_lane_prefix_the_reviewer_does_not_carry_reads_the_verdict_as_missing() -> (
    None
):
    other = SETTINGS.model_copy(update={"review_lane_prefixes": ("reviewer-",)})
    rows = _Rows((_row(),))
    rig = Rig(
        InMemoryOpenPullRequests((subject("repo-a", 1),)),
        LedgerReviewVerdicts(rows, other),
    )
    assert rig.handler.handle(SWEEP).entries[0].state.value == "failure"
