# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The review scenarios S1 to S20, replayed tick by tick against the decision.

Each fixture under ``tests/fixtures/pr_landing_decision/review_scenarios/`` is
written from the TLC trace that reaches its scenario in ``LandingController.tla``
(results ``S_<n>.out``). The fixture lists that trace, and each tick names the
trace steps it covers, so the model and the tests exercise the same
interleaving; ``test_fixture_follows_its_tla_trace`` checks the correspondence
mechanically. The scenario run itself checks every tick's expected actions and
records, and the model's safety properties (P1, P2, P3, P7, P8, P9 and the
pinned-merge property) after every tick.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.unit.nodes.node_pr_landing_decision_compute.landing_world import run_scenario

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


@pytest.mark.unit
@pytest.mark.parametrize("spec", SPECS, ids=[s["id"] for s in SPECS])
def test_review_scenario(spec: dict[str, Any]) -> None:
    run_scenario(spec)


@pytest.mark.unit
def test_every_review_scenario_has_a_fixture() -> None:
    covered = {spec["scenario"] for spec in SPECS}
    assert covered == {f"S{n}" for n in range(1, 21)}


@pytest.mark.unit
def test_every_tlc_trace_has_a_fixture() -> None:
    used = {_trace_ref(spec) for spec in SPECS}
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
    trace = TLC_TRACES[_trace_ref(spec)]
    assert spec["tla_trace"] == trace, "the fixture's trace is not the TLC trace"
    steps = [step for tick in spec["ticks"] for step in tick.get("tla", [])]
    if spec.get("trace_prefix"):
        trace = trace[: len(steps)]
    commutes = {frozenset(pair) for pair in spec.get("commutes", [])}
    assert _reachable(steps, trace, commutes), f"ticks cover {steps}, trace is {trace}"
