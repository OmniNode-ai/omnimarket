# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A reviewer red that already had its one rerun at this head is the PR's own red (OMN-19998).

The scenario is omniclaude#2752 as the watcher read it on 2026-10-14: head 6b3bf7bcb3, both hostile-review
checks completed ``failure`` after the head's one rerun (the reviewer came back blocked with one blocking
finding). The rule used to present that head as ``pending`` until the head moved, so the decision sent no
worker and raised no escalation and the PR sat with no next action. Through the rule and then the decision
node, as the controller runs them, the head now gets a fix worker told of the reviewer checks (whose job log,
the blocking finding, the worker's CI context carries), or an escalation once the worker ladder is spent.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from omnimarket.nodes.node_pr_landing_decision_compute.handlers.handler_pr_landing_decision import (
    decide_landing,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.enum_landing import (
    EnumLandingActionKind,
    EnumLandingBriefClass,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_facts import (
    ModelLandingFacts,
    ModelLandingPrFacts,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_state import (
    ModelLandingControllerState,
    ModelLandingPrRecord,
)
from tests.unit.nodes.node_pr_lifecycle_triage_compute import (
    landing_rules_adapter as lf,
)

pytestmark = pytest.mark.unit

PR = "OmniNode-ai/omniclaude#2752"
HEAD = "6b3bf7bcb3f37b9188d00aba8467d68e6312ce2d"
REVIEWER_CHECKS = ["Hostile Review Gate", "Hostile Reviewer (adversarial gate)"]
RUNS = [
    ["Hostile Review Gate", "completed", "failure", "2026-10-14T19:14:40Z"],
    [
        "Hostile Reviewer (adversarial gate)",
        "completed",
        "failure",
        "2026-10-14T19:14:30Z",
    ],
]
NOW = datetime(2026, 10, 14, 19, 50, tzinfo=UTC)


def _pr_2752() -> dict[str, Any]:
    """omniclaude#2752's facts as the controller builds them: a reviewer-pool red at its head."""
    red_class = lf.classify_red(REVIEWER_CHECKS, RUNS, companion_merged=False)
    return {
        "pr": PR,
        "head_sha": HEAD,
        "state": "open",
        "ci": "red",
        "red_class": red_class,
        "red_checks": list(REVIEWER_CHECKS),
        "created_at": "2026-10-14T17:10:36Z",
    }


def _decide(pr: dict[str, Any], record: ModelLandingPrRecord) -> Any:
    return decide_landing(
        ModelLandingFacts(
            tick=2,
            observed_at=NOW,
            state=ModelLandingControllerState(last_tick=1, records=(record,)),
            prs=(ModelLandingPrFacts.model_validate(pr),),
        )
    )


def test_2752_is_a_reviewer_pool_red() -> None:
    assert _pr_2752()["red_class"] == "reviewer_pool"


def test_2752_after_its_rerun_is_a_real_red_with_the_reviewer_checks() -> None:
    rec = [{"pr": PR, "rerun_heads": [HEAD]}]
    out = lf.apply_reviewer_pool([_pr_2752()], state_records=rec, in_flight=0)
    assert out[0]["ci"] == "red"  # never pending
    assert out[0]["red_class"] == "product"
    assert (
        out[0]["red_checks"] == REVIEWER_CHECKS
    )  # the worker is told of the blocking reviewer


def test_2752_after_its_rerun_is_real_even_with_every_reviewer_slot_taken() -> None:
    """The rerun is spent, so no slot is needed: a full reviewer endpoint never parks it as pending."""
    rec = [{"pr": PR, "rerun_heads": [HEAD]}]
    out = lf.apply_reviewer_pool([_pr_2752()], state_records=rec, in_flight=9)
    assert (out[0]["ci"], out[0]["red_class"]) == ("red", "product")


def test_2752_after_its_rerun_gets_a_fix_worker_from_the_decision() -> None:
    rec = [{"pr": PR, "rerun_heads": [HEAD]}]
    pr = lf.apply_reviewer_pool([_pr_2752()], state_records=rec, in_flight=0)[0]
    decision = _decide(pr, ModelLandingPrRecord(pr=PR, rerun_heads=(HEAD,)))
    dispatch = [
        a for a in decision.actions if a.kind is EnumLandingActionKind.DISPATCH_WORKER
    ]
    assert [(a.subject, a.head_sha) for a in dispatch] == [(PR, HEAD)]
    assert dispatch[0].brief is not None
    assert dispatch[0].brief.brief_class is EnumLandingBriefClass.REAL_RED
    assert not [a for a in decision.actions if a.kind is EnumLandingActionKind.RERUN]
    lease = next(x for x in decision.next_state.leases if x.pr == PR)
    assert lease.dispatch_red_checks == tuple(sorted(REVIEWER_CHECKS))


def test_2752_after_its_rerun_escalates_when_the_worker_ladder_is_spent() -> None:
    rec = [{"pr": PR, "rerun_heads": [HEAD]}]
    pr = lf.apply_reviewer_pool([_pr_2752()], state_records=rec, in_flight=0)[0]
    decision = _decide(
        pr,
        ModelLandingPrRecord(
            pr=PR,
            rerun_heads=(HEAD,),
            awaiting_head=HEAD,
            attempt_red_checks=tuple(REVIEWER_CHECKS),
            ladder_index=2,
        ),
    )
    assert not [
        a for a in decision.actions if a.kind is EnumLandingActionKind.DISPATCH_WORKER
    ]
    assert [(d.reason.value, d.subject) for d in decision.degraded] == [
        ("escalation_exhausted", PR)
    ]


def test_2752_before_its_rerun_still_earns_the_one_rerun() -> None:
    out = lf.apply_reviewer_pool([_pr_2752()], state_records=[], in_flight=0)
    assert out[0]["red_class"] == "runner_saturation"
    decision = _decide(out[0], ModelLandingPrRecord(pr=PR))
    assert [(a.kind, a.subject) for a in decision.actions] == [
        (EnumLandingActionKind.RERUN, PR)
    ]
