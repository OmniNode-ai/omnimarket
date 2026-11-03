# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The 57 target rows of the host bring-up orchestrator contract (OMN-21883).

Binds the generated contract to the plan: 4, 7, 15, 8, 7, 9 and 7 targets across
stages A to G, every row loading through ``load_bringup_graph`` with a check, a
falsifier and an enum action, a planted row without a falsifier refused, and no
host, address, path or version anywhere in the contract. The generator that
writes the rows from the plan text is tested on a plan excerpt fixture, so the
suite needs no copy of the plan.
"""

from __future__ import annotations

import copy
import re
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_host_bringup_orchestrator.runtime_generate_target_rows import (
    PlanShapeError,
    build_contract_text,
    parse_plan_rows,
)
from omnimarket.nodes.node_host_bringup_plan_compute.handlers.handler_host_bringup_plan import (
    HandlerHostBringupPlan,
    load_bringup_graph,
)
from omnimarket.nodes.node_host_bringup_plan_compute.models import (
    EnumBringupAction,
    EnumBringupCheckKind,
    EnumObservedState,
    EnumPlanStatus,
    EnumStepDecision,
    ModelCheckObservation,
    ModelHostBringupPlanRequest,
)

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[3]
_CONTRACT = _REPO / "src/omnimarket/nodes/node_host_bringup_orchestrator/contract.yaml"
_EXCERPT = _REPO / "tests/fixtures/host_bringup/plan_excerpt.md"

_STAGE_COUNTS = {"A": 4, "B": 7, "C": 15, "D": 8, "E": 7, "F": 9, "G": 7}
_TOP_TARGETS = {"up", "storage-up"}


def _contract() -> dict[str, Any]:
    loaded = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _row(contract: dict[str, Any], name: str) -> dict[str, Any]:
    for row in contract["targets"]:
        if row["name"] == name:
            assert isinstance(row, dict)
            return row
    raise AssertionError(name)


def _plan(observations: dict[str, EnumObservedState]) -> Any:
    return HandlerHostBringupPlan().handle(
        ModelHostBringupPlanRequest(
            graph=load_bringup_graph(_contract()),
            observations=tuple(
                ModelCheckObservation(target=n, state=s)
                for n, s in observations.items()
            ),
        )
    )


# --- the 57 rows -------------------------------------------------------------


def test_target_rows_count_per_stage() -> None:
    graph = load_bringup_graph(_contract())
    assert len(graph.targets) == 57
    assert dict(sorted(Counter(t.stage for t in graph.targets).items())) == (
        _STAGE_COUNTS
    )


def test_target_rows_every_row_loads_with_check_falsifier_and_action() -> None:
    graph = load_bringup_graph(_contract())
    names = [t.name for t in graph.targets]
    assert len(set(names)) == 57
    for target in graph.targets:
        assert target.check.kind in EnumBringupCheckKind
        assert target.action in EnumBringupAction
        assert target.falsifier.strip()
    assert {t.name for t in graph.targets if t.top} == _TOP_TARGETS


def test_target_rows_stage_order_follows_the_plan() -> None:
    stages = [t["stage"] for t in _contract()["targets"]]
    assert stages == sorted(stages)
    first = {s: stages.index(s) for s in _STAGE_COUNTS}
    assert list(first.values()) == sorted(first.values())


def test_target_rows_every_enum_member_is_used() -> None:
    graph = load_bringup_graph(_contract())
    assert {t.check.kind for t in graph.targets} == set(EnumBringupCheckKind)
    assert {t.action for t in graph.targets} == set(EnumBringupAction)


def test_target_rows_person_step_is_exactly_the_person_rows() -> None:
    graph = load_bringup_graph(_contract())
    person = {
        t.name for t in graph.targets if t.action is EnumBringupAction.PERSON_STEP
    }
    assert person == {
        "reservations",
        "ilo-access",
        "volumes",
        "os-installed",
        "host-key",
        "key-login",
        "root-access",
        "nvme-fitted",
    }


# --- the plan over the real graph --------------------------------------------


def test_target_rows_graph_plans_with_no_cycle_orphan_or_unknown_need() -> None:
    plan = _plan({})
    assert plan.status is EnumPlanStatus.PLANNED
    assert plan.refusals == ()
    assert len(plan.steps) == 57
    order = {s.target: s.order for s in plan.steps}
    for row in _contract()["targets"]:
        for need in row["needs"]:
            assert order[need] < order[row["name"]]


def test_target_rows_nothing_observed_acts_on_every_target() -> None:
    plan = _plan({})
    assert {s.decision for s in plan.steps} == {EnumStepDecision.ACT}
    assert {s.observed for s in plan.steps} == {EnumObservedState.UNKNOWN}


def test_target_rows_all_observed_ok_skips_every_target() -> None:
    names = [t["name"] for t in _contract()["targets"]]
    plan = _plan(dict.fromkeys(names, EnumObservedState.OK))
    assert {s.decision for s in plan.steps} == {EnumStepDecision.SKIP}


def test_target_rows_one_not_ok_target_is_the_only_act_when_the_rest_are_ok() -> None:
    names = [t["name"] for t in _contract()["targets"]]
    observed = dict.fromkeys(names, EnumObservedState.OK)
    observed["ram-filesystem"] = EnumObservedState.NOT_OK
    plan = _plan(observed)
    acting = [s.target for s in plan.steps if s.decision is EnumStepDecision.ACT]
    assert acting == ["ram-filesystem"]


def test_target_rows_dropping_the_top_target_refuses_the_orphans() -> None:
    contract = _contract()
    contract["targets"] = [t for t in contract["targets"] if t["name"] != "storage-up"]
    for row in contract["targets"]:
        row["needs"] = [n for n in row["needs"] if n != "storage-up"]
    plan = HandlerHostBringupPlan().handle(
        ModelHostBringupPlanRequest(graph=load_bringup_graph(contract))
    )
    assert plan.status is EnumPlanStatus.REFUSED
    unreached = {name for r in plan.refusals for name in r.names}
    assert {"nvme-fitted", "ci-capacity", "cap-raised"} <= unreached


# --- a row missing a part fails the load --------------------------------------


def test_target_rows_planted_row_without_a_falsifier_is_refused() -> None:
    contract = copy.deepcopy(_contract())
    del _row(contract, "hostname")["falsifier"]
    with pytest.raises(ValidationError, match="falsifier"):
        load_bringup_graph(contract)


@pytest.mark.parametrize("field", ["check", "action"])
def test_target_rows_planted_row_without_check_or_action_is_refused(
    field: str,
) -> None:
    contract = copy.deepcopy(_contract())
    del _row(contract, "packages")[field]
    with pytest.raises(ValidationError, match=field):
        load_bringup_graph(contract)


def test_target_rows_planted_blank_falsifier_and_unknown_action_are_refused() -> None:
    contract = copy.deepcopy(_contract())
    _row(contract, "hostname")["falsifier"] = "  "
    with pytest.raises(ValidationError):
        load_bringup_graph(contract)
    contract = copy.deepcopy(_contract())
    _row(contract, "hostname")["action"] = "reboot_everything"
    with pytest.raises(ValidationError):
        load_bringup_graph(contract)


# --- no host, address, path or version in the contract ------------------------

_FORBIDDEN = {
    "an IPv4 address": r"\b\d{1,3}(\.\d{1,3}){3}\b",
    "an absolute path": r"(^|[\s\"'(])/[A-Za-z0-9_.-]+/",
    "a home path": r"~/",
    "a host id": r"\bh\d{3}\b",
    "a version": r"\bv?\d+\.\d+\.\d+\b",
    "a host name": r"\.(lan|local|internal|ts\.net)\b",
    "a url": r"https?://",
}


@pytest.mark.parametrize("what", sorted(_FORBIDDEN))
def test_target_rows_contract_carries_no_host_address_path_or_version(
    what: str,
) -> None:
    text = _CONTRACT.read_text(encoding="utf-8")
    if what == "a version":
        # The node's own node_version is a contract field, not a target value.
        text = text.split("\ntargets:\n", 1)[1]
    assert not re.search(_FORBIDDEN[what], text), what


def test_target_rows_forbidden_patterns_do_catch_a_planted_value() -> None:
    """Positive control: each pattern fires on an example of what it forbids."""
    samples = {
        "an IPv4 address": ".".join(["10", "0", "0", "7"]),
        "an absolute path": " " + "/".join(["", "etc", "x"]),
        "a home path": "~/" + "lab",
        "a host id": "h" + "053",
        "a version": ".".join(["1", "2", "3"]),
        "a host name": "box" + ".lan",
        "a url": "https" + "://x",
    }
    assert set(samples) == set(_FORBIDDEN)
    for what, sample in samples.items():
        assert re.search(_FORBIDDEN[what], sample), what


# --- the generator ------------------------------------------------------------


def test_generator_reads_only_the_graph_section_of_the_plan() -> None:
    rows = parse_plan_rows(_EXCERPT.read_text(encoding="utf-8"))
    assert [(r.number, r.name, r.stage) for r in rows] == [
        (1, "card", "A"),
        (2, "ssh-config", "A"),
        (3, "reservations", "B"),
        (4, "host-key", "B"),
    ]
    assert rows[0].needs == ()
    assert rows[3].needs == ("reservations", "ssh-config")
    assert rows[0].falsifier == "a row with no role is refused and names the field"


def test_generator_renders_rows_that_load() -> None:
    text = build_contract_text(
        parse_plan_rows(_EXCERPT.read_text(encoding="utf-8")), expect_shape=False
    )
    contract = yaml.safe_load(text)
    assert [r["name"] for r in contract["targets"]] == [
        "card",
        "ssh-config",
        "reservations",
        "host-key",
    ]
    assert contract["targets"][0]["action"] == "stop"
    assert contract["targets"][2]["action"] == "person_step"
    assert text == build_contract_text(
        parse_plan_rows(_EXCERPT.read_text(encoding="utf-8")), expect_shape=False
    )


def test_generator_refuses_a_plan_that_does_not_match_the_declared_shape() -> None:
    rows = parse_plan_rows(_EXCERPT.read_text(encoding="utf-8"))
    with pytest.raises(PlanShapeError, match="57"):
        build_contract_text(rows)


def test_generator_refuses_a_row_it_cannot_classify() -> None:
    plan = _EXCERPT.read_text(encoding="utf-8").replace("`card`", "`stranger`")
    with pytest.raises(PlanShapeError, match="stranger"):
        build_contract_text(parse_plan_rows(plan), expect_shape=False)


def test_generator_refuses_a_malformed_row() -> None:
    plan = _EXCERPT.read_text(encoding="utf-8").replace(
        "| a row with no role is refused and names the field |", "|"
    )
    with pytest.raises(PlanShapeError, match="card"):
        parse_plan_rows(plan)


def test_generator_refuses_an_action_the_plan_text_contradicts() -> None:
    plan = _EXCERPT.read_text(encoding="utf-8").replace(
        "person: make the reservation, then confirm it once",
        "write one managed include on the launching host",
    )
    with pytest.raises(PlanShapeError, match="reservations"):
        build_contract_text(parse_plan_rows(plan), expect_shape=False)


def test_generator_refuses_a_plan_with_no_graph_section() -> None:
    with pytest.raises(PlanShapeError, match="graph"):
        parse_plan_rows("# nothing here\n")
