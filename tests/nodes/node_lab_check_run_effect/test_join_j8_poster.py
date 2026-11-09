# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""J8 from the posting side (OMN-22950): what this node posts is what the landing decision reads.

Piece 8's chain is this node's own (not a fixture); piece 9's is the landing decision's join handler. The runs this
node left on the head are turned into the decision's facts and read by ``read_lab_check`` and ``decide_landing``:

* parity: this node's reading of a head and the decision's reading of the same runs name the same verdict;
* golden: a post, then a merge of that head, reads ``golden_only`` on the floor's receipt fold;
* error: a head whose check never posted holds the head and reads ``error_only``; both together read ``passed``;
* break: a decision that ignored the attempt identity would merge the head this node invalidated (shown failing).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from omnimarket.handlers.emission_sink import InMemoryEmissionSink
from omnimarket.models.floor_17a import (
    ModelFloorReceiptInput,
    ModelJoinDef,
    ModelNodeEmission,
    ModelPieceDef,
)
from omnimarket.nodes.node_floor_emission_compute.handlers.handler_floor_receipt import (
    HandlerFloorReceipt,
)
from omnimarket.nodes.node_lab_check_run_effect.adapters.inmemory_check_run_api import (
    InMemoryCheckRunApi,
)
from omnimarket.nodes.node_lab_check_run_effect.handlers.handler_lab_check_run_effect import (
    HandlerLabCheckRunEffect,
)
from omnimarket.nodes.node_lab_check_run_effect.models.model_lab_check_run import (
    EnumLabCheckRunConclusion,
    EnumLabCheckRunOperation,
    EnumLabCheckRunOutcome,
    ModelCheckRunRecord,
    ModelLabCheckRunRequest,
)
from omnimarket.nodes.node_pr_landing_decision_compute.handlers.handler_landing_join_j8 import (
    HandlerLandingJoinJ8,
    load_join_j8_policy,
)
from omnimarket.nodes.node_pr_landing_decision_compute.handlers.handler_pr_landing_decision import (
    decide_landing,
)
from omnimarket.nodes.node_pr_landing_decision_compute.lab_check import read_lab_check
from omnimarket.nodes.node_pr_landing_decision_compute.models.enum_landing import (
    EnumLandingActionKind,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_facts import (
    ModelLandingFacts,
    ModelLandingPolicy,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_join_j8 import (
    EnumJoinJ8Outcome,
    ModelJoinJ8Request,
)
from tests.nodes.node_lab_check_run_effect.test_golden_chain_lab_check_run_effect import (
    CHECK,
    CURRENT,
    HEAD,
    NOW,
    OLD,
    OLD_HEAD,
    REPO,
    DownApi,
    overlay,
    record,
    request,
)

pytestmark = pytest.mark.unit

PR = "OmniNode-ai/omnimarket#9001"
POLICY = load_join_j8_policy()
WAIT = ModelLandingPolicy().lab_check_wait_seconds


def _decision_run(r: ModelCheckRunRecord) -> dict[str, Any]:
    """A run as the collector hands it to the decision: pending while open, else its success or failure."""
    if r.status != "completed":
        conclusion = "pending"
    else:
        conclusion = "success" if r.conclusion == "success" else "failure"
    return {
        "name": r.name,
        "head_sha": r.head_sha,
        "attempt_id": r.attempt_id,
        "conclusion": conclusion,
    }


def _facts(api: InMemoryCheckRunApi, *, dispatched_ago: int) -> ModelLandingFacts:
    return ModelLandingFacts.model_validate(
        {
            "tick": 5,
            "observed_at": NOW.isoformat(),
            "prs": [
                {
                    "pr": PR,
                    "head_sha": HEAD,
                    "state": "open",
                    "ci": "green",
                    "merge_state": "clean",
                    "created_at": "2026-11-08T11:00:00Z",
                    "lab_check": {
                        "check_name": CHECK,
                        "current_attempt_id": CURRENT,
                        "runs": [_decision_run(r) for r in api.runs if r.attempt_id],
                        "dispatched_at": (
                            NOW - timedelta(seconds=dispatched_ago)
                        ).isoformat(),
                    },
                }
            ],
            "state": {"last_tick": 4, "records": []},
        }
    )


def _merges(facts: ModelLandingFacts) -> list[str]:
    return [
        a.head_sha or ""
        for a in decide_landing(facts).actions
        if a.kind is EnumLandingActionKind.MERGE and a.subject == PR
    ]


def _fold(emissions: list[ModelNodeEmission]) -> tuple[str, list[str]]:
    receipts = HandlerFloorReceipt().handle(
        ModelFloorReceiptInput(
            now=NOW + timedelta(minutes=5),
            emissions=tuple(emissions),
            pieces=tuple(
                ModelPieceDef(piece=p, name=f"piece {p}", reading_max_age_s=86400)
                for p in (POLICY.from_piece, POLICY.to_piece)
            ),
            joins=(
                ModelJoinDef(
                    id=POLICY.join_id,
                    from_piece=POLICY.from_piece,
                    to_piece=POLICY.to_piece,
                    label="ci to landing",
                ),
            ),
            freshness_max_age_s=86400,
            error_max_age_s=86400,
            chain_deadline_s=900,
        )
    )
    join = next(j for j in receipts.joins if j.join == POLICY.join_id)
    return join.state, [f.code for f in receipts.findings]


class Rig:
    """This node and the landing join on one emission sink."""

    def __init__(self, api: InMemoryCheckRunApi | None = None) -> None:
        self.api = api or InMemoryCheckRunApi()
        self.sink = InMemoryEmissionSink()
        self.poster = HandlerLabCheckRunEffect(
            overlay=overlay(wait_bound_seconds=WAIT),
            api=self.api,
            sink=self.sink,
            clock=lambda: NOW,
        )
        self.join = HandlerLandingJoinJ8(
            sink=self.sink, policy=POLICY, clock=lambda: NOW, backend="in_memory"
        )

    def land(self, cid: str, *, dispatched_ago: int = 120) -> Any:
        return self.join.handle(
            ModelJoinJ8Request(
                correlation_id=cid,
                pr=PR,
                facts=_facts(self.api, dispatched_ago=dispatched_ago),
            )
        )


# --------------------------------------------------------------------------------------------------- parity


@pytest.mark.parametrize(
    "seed",
    [
        [],
        [record(1, OLD)],
        [record(1, CURRENT)],
        [record(1, CURRENT, "in_progress", None)],
        [record(1, CURRENT, conclusion="failure")],
        [record(1, CURRENT, head=OLD_HEAD)],
        [record(1, OLD), record(2, CURRENT)],
    ],
)
@pytest.mark.parametrize("dispatched_ago", [120, 900, 901])
def test_parity_this_nodes_reading_equals_the_decisions_on_the_same_runs(
    seed: list[ModelCheckRunRecord], dispatched_ago: int
) -> None:
    rig = Rig(InMemoryCheckRunApi(seed))
    mine = rig.poster.handle(
        request(
            operation=EnumLabCheckRunOperation.READ,
            attempt=None,
            conclusion=None,
            dispatched_ago=dispatched_ago,
        )
    ).verdict
    facts = _facts(rig.api, dispatched_ago=dispatched_ago)
    theirs = read_lab_check(facts.prs[0], NOW, ModelLandingPolicy())
    assert theirs is not None
    assert mine is theirs.verdict


# --------------------------------------------------------------------------------------------------- golden


def test_golden_a_posted_pass_is_merged_and_the_join_reads_golden_on_the_floor() -> (
    None
):
    rig = Rig()
    posted = rig.poster.handle(request(cid="j8p-golden"))
    assert posted.outcome is EnumLabCheckRunOutcome.POSTED
    result = rig.land("j8p-golden")
    assert result.outcome is EnumJoinJ8Outcome.MERGEABLE
    assert result.merge_head == HEAD
    assert _merges(_facts(rig.api, dispatched_ago=120)) == [HEAD]
    state, findings = _fold(rig.sink.events)
    assert state == "golden_only", findings
    assert findings == []


# --------------------------------------------------------------------------------------------------- error


def test_error_a_check_that_never_posted_holds_the_head_and_reads_error_on_the_floor() -> (
    None
):
    rig = Rig()
    rig.poster = HandlerLabCheckRunEffect(
        overlay=overlay(wait_bound_seconds=WAIT),
        api=DownApi(),
        sink=rig.sink,
        clock=lambda: NOW,
    )
    posted = rig.poster.handle(request(cid="j8p-error"))
    assert posted.outcome is EnumLabCheckRunOutcome.FAILED_API
    result = rig.land("j8p-error", dispatched_ago=WAIT + 1)
    assert result.outcome is EnumJoinJ8Outcome.HELD_MISSING
    assert result.merge_head is None
    state, findings = _fold(rig.sink.events)
    assert state == "error_only", findings
    assert findings == []


def test_error_an_invalidated_superseded_pass_holds_the_head_as_superseded() -> None:
    rig = Rig(InMemoryCheckRunApi([record(1, OLD)]))
    refused = rig.poster.handle(request(attempt=OLD, cid="j8p-superseded"))
    assert refused.outcome is EnumLabCheckRunOutcome.REFUSED_SUPERSEDED_ATTEMPT
    result = rig.land("j8p-superseded")
    assert result.outcome is EnumJoinJ8Outcome.HELD_SUPERSEDED
    assert _merges(_facts(rig.api, dispatched_ago=120)) == []


def test_error_the_golden_and_the_error_chain_together_read_passed() -> None:
    rig = Rig()
    rig.poster.handle(request(cid="pair-golden"))
    rig.land("pair-golden")
    down = Rig()
    down.sink = rig.sink
    down.poster = HandlerLabCheckRunEffect(
        overlay=overlay(wait_bound_seconds=WAIT),
        api=DownApi(),
        sink=rig.sink,
        clock=lambda: NOW,
    )
    down.join = HandlerLandingJoinJ8(
        sink=rig.sink, policy=POLICY, clock=lambda: NOW, backend="in_memory"
    )
    down.poster.handle(request(cid="pair-error"))
    down.land("pair-error", dispatched_ago=WAIT + 1)
    state, findings = _fold(rig.sink.events)
    assert state == "passed", findings


# --------------------------------------------------------------------------------------------------- break


def test_control_break_a_decision_that_ignores_the_attempt_would_merge_the_invalidated_head() -> (
    None
):
    """Without this node's invalidation, the superseded attempt's pass stands on the head; the decision's attempt check
    is what holds it. A decision that counted any attempt (injected here by relabelling the run) merges it."""
    rig = Rig(InMemoryCheckRunApi([record(1, OLD)]))
    held = _facts(rig.api, dispatched_ago=120)
    assert _merges(held) == [], "the real decision holds a pass of another attempt"
    relabelled = InMemoryCheckRunApi([record(1, CURRENT)])
    assert _merges(_facts(relabelled, dispatched_ago=120)) == [HEAD]


def test_control_a_request_that_is_a_post_must_name_its_attempt_and_state() -> None:
    with pytest.raises(ValueError, match="names the attempt"):
        ModelLabCheckRunRequest(
            correlation_id="c",
            operation=EnumLabCheckRunOperation.POST,
            repository=REPO,
            head_sha=HEAD,
            current_attempt_id=CURRENT,
            dispatched_at=NOW,
            now=NOW,
            conclusion=EnumLabCheckRunConclusion.SUCCESS,
        )
