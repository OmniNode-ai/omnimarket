# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Host bring-up plan compute node (OMN-21883).

AC1 graph_refusals: a cycle in needs is refused naming the cycle, a needs naming
no target is refused naming it, and a target no top target reaches is refused.
AC2 unknown_and_determinism: unknown never yields ok for any check kind, and the
same inputs give byte-equal output on two runs.
AC3 row_completeness: every row has a check, a falsifier and an enum action, and
a row missing one fails contract load.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_host_bringup_plan_compute.handlers.handler_host_bringup_plan import (
    HandlerHostBringupPlan,
    load_bringup_graph,
)
from omnimarket.nodes.node_host_bringup_plan_compute.models import (
    EnumBringupCheckKind,
    EnumObservedState,
    EnumPlanStatus,
    EnumRefusalKind,
    EnumStepDecision,
    ModelCheckObservation,
    ModelHostBringupPlanRequest,
)

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[3]
_FIXTURES = _REPO / "tests" / "fixtures" / "host_bringup"
_FIXTURE_GRAPH = _FIXTURES / "fixture_graph.yaml"
_SNAPSHOT = _FIXTURES / "fixture_plan_snapshot.json"
_NODE_CONTRACT = (
    _REPO / "src/omnimarket/nodes/node_host_bringup_plan_compute/contract.yaml"
)


def _rows() -> dict[str, Any]:
    loaded = yaml.safe_load(_FIXTURE_GRAPH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _plan(
    contract: dict[str, Any],
    observations: dict[str, EnumObservedState] | None = None,
) -> Any:
    request = ModelHostBringupPlanRequest(
        graph=load_bringup_graph(contract),
        observations=tuple(
            ModelCheckObservation(target=name, state=state)
            for name, state in (observations or {}).items()
        ),
    )
    return HandlerHostBringupPlan().handle(request)


def _row(contract: dict[str, Any], name: str) -> dict[str, Any]:
    for row in contract["targets"]:
        if row["name"] == name:
            assert isinstance(row, dict)
            return row
    raise AssertionError(name)


# --- AC1 ---------------------------------------------------------------------


def test_graph_refusals_fixture_graph_plans() -> None:
    """Positive control: the clean fixture graph is planned, not refused."""
    plan = _plan(_rows())
    assert plan.status is EnumPlanStatus.PLANNED
    assert plan.refusals == ()
    assert len(plan.steps) == 6


def test_graph_refusals_cycle_is_refused_naming_the_cycle() -> None:
    contract = _rows()
    _row(contract, "os_packages")["needs"] = ["runtime_env_file"]
    plan = _plan(contract)
    assert plan.status is EnumPlanStatus.REFUSED
    assert plan.steps == ()
    cycles = [r for r in plan.refusals if r.kind is EnumRefusalKind.CYCLE]
    assert len(cycles) == 1
    assert cycles[0].names == ("os_packages", "runtime_env_file", "os_packages")
    assert "os_packages -> runtime_env_file -> os_packages" in cycles[0].message


def test_graph_refusals_self_need_is_a_cycle() -> None:
    contract = _rows()
    _row(contract, "tailnet_joined")["needs"].append("tailnet_joined")
    plan = _plan(contract)
    cycles = [r for r in plan.refusals if r.kind is EnumRefusalKind.CYCLE]
    assert [c.names for c in cycles] == [("tailnet_joined", "tailnet_joined")]


def test_graph_refusals_unknown_need_is_refused_naming_it() -> None:
    contract = _rows()
    _row(contract, "container_engine")["needs"].append("gpu_driver")
    plan = _plan(contract)
    assert plan.status is EnumPlanStatus.REFUSED
    unknown = [r for r in plan.refusals if r.kind is EnumRefusalKind.UNKNOWN_NEED]
    assert [r.names for r in unknown] == [("container_engine", "gpu_driver")]
    assert "gpu_driver" in unknown[0].message


def test_graph_refusals_unreached_target_is_refused_naming_it() -> None:
    contract = _rows()
    contract["targets"].append(
        {
            "name": "stray_target",
            "stage": "C",
            "needs": ["os_packages"],
            "check": {"kind": "file_present", "subject": "stray"},
            "falsifier": "the stray file is absent",
            "action": "write_file",
        }
    )
    plan = _plan(contract)
    assert plan.status is EnumPlanStatus.REFUSED
    unreached = [r for r in plan.refusals if r.kind is EnumRefusalKind.UNREACHED_TARGET]
    assert [r.names for r in unreached] == [("stray_target",)]
    assert "stray_target" in unreached[0].message


def test_graph_refusals_no_top_target_and_duplicates() -> None:
    contract = _rows()
    _row(contract, "runtime_stack")["top"] = False
    plan = _plan(contract)
    kinds = {r.kind for r in plan.refusals}
    assert EnumRefusalKind.NO_TOP_TARGET in kinds

    contract = _rows()
    contract["targets"].append(copy.deepcopy(_row(contract, "os_packages")))
    plan = _plan(contract)
    dupes = [r for r in plan.refusals if r.kind is EnumRefusalKind.DUPLICATE_TARGET]
    assert [r.names for r in dupes] == [("os_packages",)]


def test_graph_refusals_observation_for_no_target_is_refused() -> None:
    plan = _plan(_rows(), {"gpu_driver": EnumObservedState.OK})
    assert plan.status is EnumPlanStatus.REFUSED
    assert [r.names for r in plan.refusals] == [("gpu_driver",)]


# --- AC2 ---------------------------------------------------------------------


@pytest.mark.parametrize("kind", list(EnumBringupCheckKind))
def test_unknown_and_determinism_unknown_never_ok(kind: EnumBringupCheckKind) -> None:
    contract = _rows()
    for row in contract["targets"]:
        row["check"]["kind"] = kind.value
    everything_ok = {r["name"]: EnumObservedState.OK for r in contract["targets"]}

    # Positive control: an observed OK skips the target.
    ok_plan = _plan(contract, everything_ok)
    assert {s.decision for s in ok_plan.steps} == {EnumStepDecision.SKIP}

    # Explicit unknown, and an absent observation, both act and never read as ok.
    explicit = dict(everything_ok, runtime_env_file=EnumObservedState.UNKNOWN)
    absent = {k: v for k, v in everything_ok.items() if k != "runtime_env_file"}
    for observations in (explicit, absent):
        plan = _plan(contract, observations)
        step = next(s for s in plan.steps if s.target == "runtime_env_file")
        assert step.check_kind is kind
        assert step.observed is EnumObservedState.UNKNOWN
        assert step.decision is EnumStepDecision.ACT


def test_unknown_and_determinism_byte_equal_and_order_free() -> None:
    contract = _rows()
    observations = {
        "os_packages": EnumObservedState.OK,
        "tailnet_joined": EnumObservedState.NOT_OK,
    }
    first = _plan(contract, observations).model_dump_json()
    second = _plan(copy.deepcopy(contract), observations).model_dump_json()
    assert first.encode() == second.encode()

    shuffled = copy.deepcopy(contract)
    shuffled["targets"].reverse()
    for row in shuffled["targets"]:
        row["needs"] = list(reversed(row["needs"]))
    reversed_obs = dict(reversed(list(observations.items())))
    assert _plan(shuffled, reversed_obs).model_dump_json().encode() == first.encode()


def test_unknown_and_determinism_snapshot() -> None:
    plan = _plan(
        _rows(),
        {
            "os_packages": EnumObservedState.OK,
            "operator_sudo": EnumObservedState.OK,
            "tailnet_joined": EnumObservedState.NOT_OK,
        },
    )
    rendered = json.dumps(plan.model_dump(mode="json"), indent=2) + "\n"
    assert rendered == _SNAPSHOT.read_text(encoding="utf-8")


def test_unknown_and_determinism_needs_come_first() -> None:
    plan = _plan(_rows())
    order = {s.target: s.order for s in plan.steps}
    for row in _rows()["targets"]:
        for need in row["needs"]:
            assert order[need] < order[row["name"]]


# --- AC3 ---------------------------------------------------------------------


def test_row_completeness_fixture_rows_load() -> None:
    graph = load_bringup_graph(_rows())
    for target in graph.targets:
        assert target.check.kind in EnumBringupCheckKind
        assert target.falsifier.strip()
        assert target.action.value


@pytest.mark.parametrize("field", ["check", "falsifier", "action"])
def test_row_completeness_missing_field_fails_load(field: str) -> None:
    contract = _rows()
    del _row(contract, "container_engine")[field]
    with pytest.raises(ValidationError, match=field):
        load_bringup_graph(contract)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("action", "reboot_everything"),
        ("falsifier", "   "),
        ("check", {"kind": "vibes", "subject": "runtime_env"}),
        ("check", {"kind": "file_present"}),
    ],
)
def test_row_completeness_bad_field_fails_load(field: str, value: object) -> None:
    contract = _rows()
    _row(contract, "container_engine")[field] = value
    with pytest.raises(ValidationError):
        load_bringup_graph(contract)


@pytest.mark.parametrize(
    "subject",
    ["/etc/onex/runtime.env", "192.168.1.10", "host.example.lan", "v1.2.3", "Runtime"],
)
def test_row_completeness_no_host_path_or_version_in_a_row(subject: str) -> None:
    contract = _rows()
    _row(contract, "runtime_env_file")["check"]["subject"] = subject
    with pytest.raises(ValidationError):
        load_bringup_graph(contract)


def test_row_completeness_extra_key_and_missing_targets_fail_load() -> None:
    contract = _rows()
    _row(contract, "os_packages")["host"] = "anything"
    with pytest.raises(ValidationError):
        load_bringup_graph(contract)
    with pytest.raises(ValidationError):
        load_bringup_graph({"name": "no rows here"})


def test_row_completeness_node_contract_carries_no_host_path_or_version() -> None:
    text = _NODE_CONTRACT.read_text(encoding="utf-8")
    assert not re.search(r"\b\d{1,3}(\.\d{1,3}){3}\b", text)
    assert not re.search(r"(^|\s)/(etc|opt|home|usr|var|srv)/", text)
    contract = yaml.safe_load(text)
    assert contract["descriptor"]["purity"] == "pure"
