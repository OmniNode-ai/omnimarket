# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Lab models first, with a scored rubric per task class (OMN-17427).

Golden chain: the shipped routing contracts satisfy the rule, every routable
class declares its lab verdict, and a measured fail releases a class from it.

Error chain: each way a contract change could route a lab-passing class to a
cloud model first is refused, and each is proven on a mutation of the real
contract so a zero finding count is never the only evidence.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.inference.task_class_authority import load_task_class_authority
from omnimarket.models.delegation.model_lab_rubric import (
    EnumLabVerdict,
    ModelLabFirstPolicy,
    ModelLabRubric,
)
from omnimarket.nodes.node_lab_first_routing_check_compute.handlers.handler_lab_first_routing_check import (
    HandlerLabFirstRoutingCheck,
)
from omnimarket.nodes.node_lab_first_routing_check_compute.models.model_lab_first_routing_check import (
    EnumLabFirstFindingClass,
    ModelLabFirstRoutingRequest,
    ModelLabFirstRoutingResult,
)

pytestmark = pytest.mark.unit

_CONFIGS = Path("src/omnimarket/configs")
_CONTRACTS_PATH = _CONFIGS / "task_class_contracts.v1.yaml"
_TIERS_PATH = _CONFIGS / "routing_tiers.yaml"


def _load(path: Path) -> dict[str, object]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return raw


def _contracts() -> dict[str, object]:
    return copy.deepcopy(_load(_CONTRACTS_PATH))


def _tiers() -> dict[str, object]:
    return copy.deepcopy(_load(_TIERS_PATH))


def _classes(contracts: dict[str, object]) -> dict[str, dict[str, object]]:
    classes = contracts["task_classes"]
    assert isinstance(classes, dict)
    return classes


def _check(
    contracts: dict[str, object] | None = None,
    tiers: dict[str, object] | None = None,
) -> ModelLabFirstRoutingResult:
    return HandlerLabFirstRoutingCheck().handle(
        ModelLabFirstRoutingRequest(
            task_class_contracts=contracts if contracts is not None else _contracts(),
            routing_tiers=tiers if tiers is not None else _tiers(),
        )
    )


def _findings(
    result: ModelLabFirstRoutingResult,
) -> set[tuple[str, EnumLabFirstFindingClass]]:
    return {(f.task_class, f.finding_class) for f in result.findings}


def _measured(adequate: int, of: int = 28) -> dict[str, object]:
    return {
        "verdict": "pass" if adequate >= 21 else "fail",
        "measured": {
            "model": "Qwen3.8-27B",
            "adequate": adequate,
            "of": of,
            "source": "test fixture",
        },
    }


# ── golden chain ─────────────────────────────────────────────────────────────


def test_shipped_contracts_route_lab_first() -> None:
    result = _check()
    assert result.findings == ()
    assert result.ok is True
    assert result.checked_classes > 0


def test_every_routable_class_declares_its_lab_verdict() -> None:
    authority = load_task_class_authority()
    routable = [
        name
        for name, entry in authority.task_classes.items()
        if entry.routing_availability is None
    ]
    assert routable
    for name in routable:
        assert authority.task_classes[name].lab_rubric is not None, name


def test_recorded_code_review_measurement_is_a_fail_below_the_bar() -> None:
    entry = _classes(_contracts())["code_review"]
    rubric = ModelLabRubric.model_validate(entry["lab_rubric"])
    contracts = _contracts()
    policy = contracts["lab_first_policy"]
    assert isinstance(policy, dict)
    assert policy["bar"] == {"adequate": 21, "of": 28}
    assert rubric.verdict is EnumLabVerdict.FAIL
    assert rubric.measured is not None
    assert (rubric.measured.adequate, rubric.measured.of) == (0, 28)


def test_recorded_review_measurement_is_a_fail_below_the_bar() -> None:
    entry = _classes(_contracts())["review"]
    rubric = ModelLabRubric.model_validate(entry["lab_rubric"])
    contracts = _contracts()
    policy = ModelLabFirstPolicy.model_validate(contracts["lab_first_policy"])
    assert rubric.verdict is EnumLabVerdict.FAIL
    assert rubric.measured is not None
    assert rubric.measured.model == "Qwen3.8-27B"
    assert (rubric.measured.adequate, rubric.measured.of) == (25, 60)
    assert rubric.derived_verdict(policy.bar) is EnumLabVerdict.FAIL
    assert _check(contracts).ok is True


def test_a_measured_pass_keeps_the_class_lab_first() -> None:
    contracts = _contracts()
    _classes(contracts)["code_generation"]["lab_rubric"] = _measured(25)
    assert _check(contracts).ok is True


def test_a_measured_fail_releases_a_class_from_lab_first() -> None:
    contracts = _contracts()
    entry = _classes(contracts)["research"]
    entry["lab_rubric"] = _measured(9)
    entry["escalation_policy"]["tier_order"] = ["cheap_cloud", "claude", "local"]
    assert _check(contracts).ok is True


def test_a_class_with_no_serving_tier_needs_no_rubric() -> None:
    contracts = _contracts()
    entry = _classes(contracts)["agent_delegation"]
    assert "routing_availability" in entry
    entry.pop("lab_rubric", None)
    assert _check(contracts).ok is True


def test_cloud_first_tier_order_is_refused_for_a_measured_pass() -> None:
    contracts = _contracts()
    entry = _classes(contracts)["code_generation"]
    entry["lab_rubric"] = _measured(25)
    entry["escalation_policy"]["tier_order"] = ["cheap_cloud", "local", "claude"]
    assert _findings(_check(contracts)) == {
        ("code_generation", EnumLabFirstFindingClass.CLOUD_FIRST_TIER_ORDER)
    }


def test_cloud_first_tier_order_is_refused_for_an_unmeasured_class() -> None:
    contracts = _contracts()
    entry = _classes(contracts)["summarization"]
    assert entry["lab_rubric"]["verdict"] == "unmeasured"
    entry["escalation_policy"]["tier_order"] = ["cheap_frontier", "local"]
    assert _findings(_check(contracts)) == {
        ("summarization", EnumLabFirstFindingClass.CLOUD_FIRST_TIER_ORDER)
    }


def test_a_tier_order_that_omits_the_lab_tier_is_refused() -> None:
    contracts = _contracts()
    entry = _classes(contracts)["test"]
    entry["escalation_policy"]["tier_order"] = ["cheap_cloud", "claude"]
    assert _findings(_check(contracts)) == {
        ("test", EnumLabFirstFindingClass.CLOUD_FIRST_TIER_ORDER)
    }


def test_an_entry_share_that_starts_a_fraction_off_the_lab_is_refused() -> None:
    contracts = _contracts()
    entry = _classes(contracts)["refactor"]
    entry["escalation_policy"]["entry_share"] = {
        "tier": "cheap_frontier",
        "share": 0.25,
    }
    assert _findings(_check(contracts)) == {
        ("refactor", EnumLabFirstFindingClass.ENTRY_SHARE_OFF_LAB)
    }


def test_a_per_class_pin_to_a_cloud_model_is_refused() -> None:
    contracts = _contracts()
    overrides = contracts["task_model_overrides"]
    assert isinstance(overrides, dict)
    overrides["reasoning"] = "gemini-2.5-flash"
    assert _findings(_check(contracts)) == {
        ("reasoning", EnumLabFirstFindingClass.NON_LAB_MODEL_PIN)
    }


def test_a_cloud_default_model_is_refused_for_a_class_with_no_pin() -> None:
    contracts = _contracts()
    overrides = contracts["task_model_overrides"]
    assert isinstance(overrides, dict)
    overrides.pop("research")
    contracts["default_task_model_ref"] = "gemini-2.5-flash"
    assert ("research", EnumLabFirstFindingClass.NON_LAB_MODEL_PIN) in _findings(
        _check(contracts)
    )


def test_a_routable_class_with_no_rubric_is_refused() -> None:
    contracts = _contracts()
    _classes(contracts)["planning"].pop("lab_rubric")
    assert _findings(_check(contracts)) == {
        ("planning", EnumLabFirstFindingClass.RUBRIC_MISSING)
    }


def test_a_verdict_its_measurement_does_not_earn_is_refused() -> None:
    contracts = _contracts()
    rubric = _measured(5)
    rubric["verdict"] = "pass"
    _classes(contracts)["review"]["lab_rubric"] = rubric
    assert _findings(_check(contracts)) == {
        ("review", EnumLabFirstFindingClass.RUBRIC_INCONSISTENT)
    }


def test_a_missing_policy_is_refused() -> None:
    contracts = _contracts()
    contracts.pop("lab_first_policy")
    assert (
        "<policy>",
        EnumLabFirstFindingClass.POLICY_INVALID,
    ) in _findings(_check(contracts))


def test_a_ladder_whose_first_tier_is_not_the_lab_is_refused() -> None:
    tiers = _tiers()
    tier_list = tiers["tiers"]
    assert isinstance(tier_list, list)
    tier_list.reverse()
    assert (
        "<policy>",
        EnumLabFirstFindingClass.POLICY_INVALID,
    ) in _findings(_check(tiers=tiers))


def test_the_contract_loader_types_the_policy_and_rubric() -> None:
    authority = load_task_class_authority()
    assert authority.lab_first_policy is not None
    assert authority.lab_first_policy.lab_tier == "local"
    assert authority.task_classes["code_review"].lab_rubric is not None


def test_a_rubric_with_a_verdict_and_no_measurement_does_not_load() -> None:
    with pytest.raises(ValidationError):
        ModelLabRubric.model_validate({"verdict": "pass"})
    with pytest.raises(ValidationError):
        ModelLabRubric.model_validate(
            {"verdict": "unmeasured", **{"measured": _measured(25)["measured"]}}
        )


def test_the_node_contract_declares_its_terminal_event_and_handler() -> None:
    node_dir = Path("src/omnimarket/nodes/node_lab_first_routing_check_compute")
    contract = _load(node_dir / "contract.yaml")
    handler = contract["handler"]
    assert isinstance(handler, dict)
    assert (
        contract["terminal_event"] == "onex.evt.omnimarket.lab-first-routing-checked.v1"
    )
    assert handler["class"] == HandlerLabFirstRoutingCheck.__name__
