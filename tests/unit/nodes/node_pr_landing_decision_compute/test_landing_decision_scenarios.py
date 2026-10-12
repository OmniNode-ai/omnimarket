# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The review scenarios S1 to S25, replayed tick by tick against the decision.

Each fixture under ``tests/fixtures/pr_landing_decision/review_scenarios/`` is
written from the TLC trace that reaches its scenario in ``LandingController.tla``
(results ``S_<n>.out``). The fixture lists that trace, and each tick names the
trace steps it covers, so the model and the tests exercise the same
interleaving; ``test_fixture_follows_its_tla_trace`` checks the correspondence
mechanically. The scenario run itself checks every tick's expected actions and
records, and the model's safety properties (P1, P2, P3, P7, P8, P9 and the
pinned-merge property) after every tick.

S1 to S20 are the scenarios the model checks. A fixture the model does not
cover says ``tla_modeled: false`` (S21 refresh and S22-S25 ledger regressions) and names
no trace: it is replayed and property-checked here only, and it never lowers
the floor of S1 to S20 that G-ACT's scenario receipt requires.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_pr_landing_decision_compute.handlers.handler_pr_landing_decision import (
    HandlerPrLandingDecision,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.enum_landing import (
    EnumLandingActionKind,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_facts import (
    ModelLandingSharedCheckVerdict,
    ModelLandingUpdateNoNewCommits,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_state import (
    ModelLandingPrRecord,
)
from tests.unit.nodes.node_pr_landing_decision_compute.landing_world import (
    LandingWorld,
    run_scenario,
    sha,
)

FIXTURES = (
    Path(__file__).resolve().parents[3]
    / "fixtures"
    / "pr_landing_decision"
    / "review_scenarios"
)

# The action sequence of each scenario's TLC trace (LandingController.tla,
# results/S_<n>.out, the lab TLC run of the model), without the initial state.
TLC_TRACES: dict[str, list[str]] = {
    "S_1": ["Dispatch", "WorkerPush", "ObserveHead"],
    "S_2": [
        "Dispatch",
        "WorkerTimeout",
        "WorkerPush",
        "ProcessExit",
        "ConfirmTerminated",
    ],
    "S_3": ["MemberClose", "RebuildRequest"],
    "S_4": ["MemberPush", "CiResult", "CiResult", "CompCi", "RebuildRequest"],
    "S_5": ["MemberClose", "MemberClose", "CompanionClose"],
    "S_6": ["CiResult", "CompCi", "CompanionMerge", "EligRerun"],
    "S_7": [
        "Dispatch",
        "WorkerPush",
        "CiResult",
        "WorkerResult",
        "RecordResult",
        "Land",
    ],
    "S_8": [
        "Dispatch",
        "WorkerResult",
        "ProcessExit",
        "RecordResult",
        "ConfirmTerminated",
        "Dispatch",
    ],
    "S_9": [
        "Dispatch",
        "ProcessExit",
        "WorkerTimeout",
        "ConfirmTerminated",
        "Dispatch",
    ],
    "S_10": [
        "Dispatch",
        "WorkerResult",
        "ProcessExit",
        "RecordResult",
        "ConfirmTerminated",
        "Dispatch",
    ],
    "S_11": [
        "Dispatch",
        "WorkerResult",
        "ProcessExit",
        "RecordResult",
        "BlockerChange",
        "ConfirmTerminated",
        "Dispatch",
    ],
    "S_12": [
        "Dispatch",
        "WorkerResult",
        "ProcessExit",
        "RepoDrain",
        "Revoke",
        "ConfirmTerminated",
        "RecordResult",
        "ObserveOnly",
    ],
    "S_12r": [
        "Dispatch",
        "WorkerResult",
        "ProcessExit",
        "RepoDrain",
        "RecordResult",
        "ConfirmTerminated",
        "ObserveOnly",
    ],
    "S_13": [
        "Dispatch",
        "WorkerPush",
        "CiResult",
        "ChildSpawn",
        "WorkerResult",
        "RecordResult",
        "Kill",
        "ConfirmTerminated",
    ],
    "S_14": [
        "MemberEligibilityChange",
        "CompCi",
        "MemberEligibilityChange",
        "MemberEligibilityChange",
        "MemberEligibilityChange",
        "CompanionMerge",
    ],
    "S_14b": [
        "MemberEligibilityChange",
        "CompCi",
        "MemberEligibilityChange",
        "MemberEligibilityChange",
        "RebuildRequest",
    ],
    "S_15": [
        "CiResult",
        "MemberEligibilityChange",
        "CompCi",
        "RebuildRequest",
        "MemberEligibilityChange",
        "RebuildDeliver",
        "ProducerSucceed",
        "RebuildSeen",
        "CompanionClose",
        "CoverRequest",
    ],
    "S_16": ["Dispatch", "WorkerPush", "CiResult", "WorkerResult", "RecordResult"],
    "S_17": [
        "Dispatch",
        "ForeignRewrite",
        "CiResult",
        "WorkerResult",
        "ProcessExit",
        "RecordResult",
        "ConfirmTerminated",
        "Dispatch",
    ],
    "S_18": [
        "MemberPush",
        "CiResult",
        "RebuildRequest",
        "RebuildDeliver",
        "ProducerFail",
        "RecordDelivered",
        "RebuildFailed",
        "RebuildRetry",
        "RebuildDeliver",
        "ProducerSucceed",
        "RebuildSeen",
    ],
    "S_18x": [
        "MemberPush",
        "RebuildRequest",
        "RebuildDeliver",
        "ProducerFail",
        "RecordDelivered",
        "RebuildFailed",
        "RebuildRetry",
        "RebuildDeliver",
        "ProducerFail",
        "RecordDelivered",
        "RebuildFailed",
        "RebuildRetry",
        "RebuildDeliver",
        "ProducerFail",
        "RecordDelivered",
        "RebuildFailed",
        "RebuildPark",
    ],
    "S_19": [
        "MemberPush",
        "RebuildRequest",
        "ControllerCrash",
        "RebuildDeliver",
        "ProducerSucceed",
        "RebuildSeen",
    ],
    "S_20": [
        "MemberPush",
        "RebuildRequest",
        "RebuildDeliver",
        "ProducerSucceed",
        "ControllerCrash",
        "RebuildDeliver",
        "ProducerSucceed",
        "RebuildSeen",
    ],
}


def _load() -> list[dict[str, Any]]:
    specs = []
    for path in sorted(FIXTURES.glob("*.yaml")):
        spec = yaml.safe_load(path.read_text())
        spec["_path"] = path.name
        specs.append(spec)
    return specs


SPECS = _load()


def _trace_ref(spec: dict[str, Any]) -> str:
    return str(spec.get("trace_ref") or "S_" + spec["scenario"][1:])


def _modeled(spec: dict[str, Any]) -> bool:
    return bool(spec.get("tla_modeled", True))


@pytest.mark.unit
@pytest.mark.parametrize("spec", SPECS, ids=[s["id"] for s in SPECS])
def test_review_scenario(spec: dict[str, Any]) -> None:
    run_scenario(spec)


@pytest.mark.unit
@pytest.mark.parametrize("mismatch", ["pr", "head", "future"])
def test_c5_no_new_commits_feedback_must_match_the_attempt(mismatch: str) -> None:
    world = LandingWorld(
        {
            "world": {
                "prs": [{"pr": "acme/app#1", "ci": "green", "merge_state": "behind"}]
            }
        }
    )
    world.tick = 2
    head = sha("acme/app#1", 1)
    world.state = world.state.model_copy(
        update={
            "records": (ModelLandingPrRecord(pr="acme/app#1", update_heads=(head,)),)
        }
    )
    response = ModelLandingUpdateNoNewCommits(
        pr="acme/app#2" if mismatch == "pr" else "acme/app#1",
        head_sha=sha("acme/app#1", 2) if mismatch == "head" else head,
        at=world.now + timedelta(seconds=1)
        if mismatch == "future"
        else world.now - timedelta(seconds=900),
    )
    facts = world.facts().model_copy(update={"update_no_new_commits": (response,)})
    decision = HandlerPrLandingDecision().handle(facts)
    assert not any(
        a.kind is EnumLandingActionKind.UPDATE_BRANCH for a in decision.actions
    )


@pytest.mark.unit
@pytest.mark.parametrize("mismatch", ["repo", "check", "future", "newer_red"])
def test_c5_shared_verdict_must_be_current_and_match_the_failed_check(
    mismatch: str,
) -> None:
    world = LandingWorld(
        {
            "world": {
                "policy": {"max_workers": 0},
                "prs": [
                    {
                        "pr": "acme/app#1",
                        "ci": "red",
                        "red_class": "cascade",
                        "red_checks": ["delegation-health-check"],
                    }
                ],
            }
        }
    )
    world.tick = 2
    head = sha("acme/app#1", 1)
    world.state = world.state.model_copy(
        update={
            "records": (ModelLandingPrRecord(pr="acme/app#1", rerun_heads=(head,)),)
        }
    )
    verdict = ModelLandingSharedCheckVerdict(
        repo="acme/other" if mismatch == "repo" else "acme/app",
        check="other-check" if mismatch == "check" else "delegation-health-check",
        verdict_id="nightly-1",
        green=True,
        at=world.now + timedelta(seconds=1)
        if mismatch == "future"
        else world.now - timedelta(seconds=300),
    )
    verdicts = (verdict,)
    if mismatch == "newer_red":
        verdicts += (
            verdict.model_copy(
                update={
                    "at": world.now,
                    "verdict_id": "nightly-2",
                    "green": False,
                }
            ),
        )
    facts = world.facts().model_copy(update={"shared_check_verdicts": verdicts})
    decision = HandlerPrLandingDecision().handle(facts)
    assert decision.actions == ()
    assert (
        HandlerPrLandingDecision()
        .handle(
            facts.model_copy(
                update={"shared_check_verdicts": tuple(reversed(verdicts))}
            )
        )
        .model_dump_json()
        == decision.model_dump_json()
    )


@pytest.mark.unit
def test_every_review_scenario_has_a_fixture() -> None:
    covered = {spec["scenario"] for spec in SPECS}
    assert covered >= {f"S{n}" for n in range(1, 21)}


@pytest.mark.unit
def test_unmodeled_scenarios_are_explicit() -> None:
    unmodeled = {spec["scenario"] for spec in SPECS if not _modeled(spec)}
    assert unmodeled == {"S21", "S22", "S23", "S24", "S25"}


@pytest.mark.unit
def test_every_tlc_trace_has_a_fixture() -> None:
    used = {_trace_ref(spec) for spec in SPECS if _modeled(spec)}
    assert used == set(TLC_TRACES)


def _reachable(
    sequence: list[str], trace: list[str], commutes: set[frozenset[str]]
) -> bool:
    """Whether ``sequence`` becomes ``trace`` by swapping adjacent commuting steps."""
    if sorted(sequence) != sorted(trace):
        return False
    work = list(sequence)
    for index, step in enumerate(trace):
        position = work.index(step, index)
        while position > index:
            pair = frozenset((work[position - 1], work[position]))
            if pair not in commutes:
                return False
            work[position - 1], work[position] = work[position], work[position - 1]
            position -= 1
    return work == trace


@pytest.mark.unit
@pytest.mark.parametrize("spec", SPECS, ids=[s["id"] for s in SPECS])
def test_fixture_follows_its_tla_trace(spec: dict[str, Any]) -> None:
    if not _modeled(spec):
        assert "tla_trace" not in spec
        assert not any(tick.get("tla") for tick in spec["ticks"])
        return
    trace = TLC_TRACES[_trace_ref(spec)]
    assert spec["tla_trace"] == trace, "the fixture's trace is not the TLC trace"
    steps = [step for tick in spec["ticks"] for step in tick.get("tla", [])]
    if spec.get("trace_prefix"):
        trace = trace[: len(steps)]
    commutes = {frozenset(pair) for pair in spec.get("commutes", [])}
    assert _reachable(steps, trace, commutes), f"ticks cover {steps}, trace is {trace}"
