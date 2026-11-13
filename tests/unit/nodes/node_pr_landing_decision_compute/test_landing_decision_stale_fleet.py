# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A stale-fleet red head earns one rerun per head and never a fix worker (OMN-20418).

The recorded case is omnibase_infra#5908 at head 84bd62c1 on 2026-11-12: its runner-label-exists check failed
because the label set was carried only by offline runners; the fleet read online later and the head was never
rerun, so fix workers were leased instead. The control is the same head classed ``product``: it keeps the worker.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_pr_landing_decision_compute.handlers.handler_pr_landing_decision import (
    decide_landing,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.enum_landing import (
    RERUNNABLE_RED_CLASSES,
    EnumLandingActionKind,
    EnumLandingRedClass,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_decision import (
    ModelLandingDecision,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_facts import (
    ModelLandingFacts,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.handlers.handler_classify_landing_red import (
    HandlerClassifyLandingRed,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.model_landing_red_facts import (
    ModelLandingRedFacts,
)

pytestmark = pytest.mark.unit

KEY = "OmniNode-ai/omnibase_infra#5908"
HEAD = "84bd62c1c75359cfb958e8cede1610a8e9263062"
NEW_HEAD = "b" * 40
OBSERVED_AT = "2026-11-12T14:20:00Z"
CREATED_AT = "2026-11-12T13:00:00Z"
RED_CHECKS = ["CI Summary", "runner-label-exists / runner-label-exists"]


def _pr(**fields: Any) -> dict[str, Any]:
    return {
        "pr": KEY,
        "head_sha": HEAD,
        "state": "open",
        "ci": "red",
        "red_class": "stale_fleet",
        "red_checks": RED_CHECKS,
        "merge_state": "blocked",
        "lacks_base_head": False,
        "suspensions": [],
        "collaborator": False,
        "created_at": CREATED_AT,
        **fields,
    }


def _facts(
    pr: dict[str, Any] | None = None,
    *,
    tick: int = 100,
    state: dict[str, Any] | None = None,
) -> ModelLandingFacts:
    return ModelLandingFacts.model_validate(
        {
            "tick": tick,
            "observed_at": OBSERVED_AT,
            "prs": [pr or _pr()],
            "state": state or {"last_tick": tick - 1},
        }
    )


def _actions(decision: ModelLandingDecision) -> list[tuple[str, str]]:
    return [(a.kind.value, a.subject) for a in decision.actions]


RERUN = EnumLandingActionKind.RERUN.value
WORKER = EnumLandingActionKind.DISPATCH_WORKER.value


def test_stale_fleet_is_a_rerunnable_class() -> None:
    assert EnumLandingRedClass.STALE_FLEET in RERUNNABLE_RED_CLASSES


def test_stale_fleet_head_gets_one_rerun_and_no_worker() -> None:
    decision = decide_landing(_facts())
    assert _actions(decision) == [(RERUN, KEY)]
    assert decision.actions[0].head_sha == HEAD
    assert decision.violations == ()


def test_failing_control_a_product_red_on_the_same_head_keeps_the_worker() -> None:
    decision = decide_landing(_facts(_pr(red_class="product")))
    assert _actions(decision) == [(WORKER, KEY)]


def test_one_rerun_per_head_then_a_new_head_earns_its_own() -> None:
    first = decide_landing(_facts())
    assert _actions(first) == [(RERUN, KEY)]
    state = first.next_state.model_dump(mode="json")

    same_head = decide_landing(_facts(tick=101, state=state))
    assert (RERUN, KEY) not in _actions(same_head)

    new_head = decide_landing(_facts(_pr(head_sha=NEW_HEAD), tick=101, state=state))
    assert _actions(new_head) == [(RERUN, KEY)]
    assert new_head.actions[0].head_sha == NEW_HEAD


# ---------------------------------------------------------------- the seam and the chain events
# Seam: the triage node's verdict string is the decision node's ``red_class`` fact. The golden chain runs the
# recorded job log through the classifier and its verdict through the decision; the error chain runs the same
# head with the facts the controller had before this change (no log, no fleet-status): it is product and a
# worker is leased. Either side changing the class name or its action breaks one of the two.

_LOG = "omnibase_infra_5908_runner_label_exists_job_131219942690.log"
_FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "landing_red_rules"
_CHECK = "runner-label-exists / runner-label-exists"
_ONLINE = (
    ("self-hosted", "omnibase-ci", "arch-amd64", "online-runner-1"),
)  # synthetic fleet reading


def _classified(**fields: Any) -> str:
    facts = ModelLandingRedFacts(
        red=tuple(RED_CHECKS),
        runs=tuple((n, "completed", "failure", "") for n in RED_CHECKS),
        companion_merged=False,
        **fields,
    )
    return str(HandlerClassifyLandingRed().handle(facts).red_class)


def test_golden_chain_recorded_log_to_one_rerun() -> None:
    log = (_FIXTURES / _LOG).read_text()
    red_class = _classified(failure_logs={_CHECK: log}, online_runner_labels=_ONLINE)
    assert red_class == "stale_fleet"
    assert EnumLandingRedClass(red_class) in RERUNNABLE_RED_CLASSES
    decision = decide_landing(_facts(_pr(red_class=red_class)))
    assert _actions(decision) == [(RERUN, KEY)]


def test_error_chain_unread_log_and_fleet_keeps_the_worker() -> None:
    red_class = _classified()
    assert red_class == "product"
    decision = decide_landing(_facts(_pr(red_class=red_class)))
    assert _actions(decision) == [(WORKER, KEY)]
