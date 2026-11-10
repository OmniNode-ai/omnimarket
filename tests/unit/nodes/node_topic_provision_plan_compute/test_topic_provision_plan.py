# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for the topic provision plan handler."""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_topic_provision_plan_compute.handlers.handler_topic_provision_plan import (
    HandlerTopicProvisionPlan,
    TopicProvisionRuleError,
    plan_topic_provision,
)
from omnimarket.nodes.node_topic_provision_plan_compute.models.enum_topic_provision import (
    EnumTopicProvisionMode,
    EnumTopicSkipReason,
)
from omnimarket.nodes.node_topic_provision_plan_compute.models.model_topic_provision_contract import (
    ModelTopicProvisionContract,
)
from omnimarket.nodes.node_topic_provision_plan_compute.models.model_topic_provision_request import (
    ModelTopicProvisionRequest,
)
from omnimarket.nodes.node_topic_provision_plan_compute.models.model_topic_provision_rule import (
    ModelTopicProvisionRule,
)

pytestmark = pytest.mark.unit


def _contracts() -> tuple[ModelTopicProvisionContract, ...]:
    return (
        ModelTopicProvisionContract(
            contract_name="delegation",
            runtime_profiles=("main",),
            topics=("a", "b"),
        ),
        ModelTopicProvisionContract(
            contract_name="board",
            runtime_profiles=("effects",),
            topics=("b", "c"),
        ),
        ModelTopicProvisionContract(
            contract_name="unlisted",
            runtime_profiles=("main",),
            topics=("d",),
        ),
    )


def _request(
    mode: EnumTopicProvisionMode,
    *,
    lane_manifest: tuple[str, ...] = ("delegation", "board"),
    live_topics: tuple[str, ...] = (),
) -> ModelTopicProvisionRequest:
    return ModelTopicProvisionRequest(
        contracts=_contracts(),
        profile="main",
        lane_manifest=lane_manifest,
        live_topics=live_topics,
        rule=ModelTopicProvisionRule(mode=mode),
    )


def test_a_contract_outside_the_profile_gets_no_topic() -> None:
    plan = plan_topic_provision(_request(EnumTopicProvisionMode.PROFILE_AND_MANIFEST))
    assert plan.topics_to_provision == ("a", "b")
    skips = {s.contract_name: s.reason for s in plan.contracts_skipped}
    assert skips["board"] == EnumTopicSkipReason.OUTSIDE_PROFILE
    assert skips["unlisted"] == EnumTopicSkipReason.NOT_IN_LANE_MANIFEST
    assert "c" not in plan.topics_to_provision
    assert "d" not in plan.topics_to_provision


def test_the_baseline_all_mode_provisions_the_outside_contracts() -> None:
    plan = plan_topic_provision(_request(EnumTopicProvisionMode.ALL))
    assert set(plan.topics_to_provision) == {"a", "b", "c", "d"}
    assert "c" in plan.topics_to_provision
    assert "d" in plan.topics_to_provision


def test_profile_mode_ignores_the_manifest() -> None:
    plan = plan_topic_provision(_request(EnumTopicProvisionMode.PROFILE))
    assert plan.topics_to_provision == ("a", "b", "d")


def test_a_shared_topic_stays_when_a_loading_contract_declares_it() -> None:
    plan = plan_topic_provision(_request(EnumTopicProvisionMode.PROFILE_AND_MANIFEST))
    assert "b" in plan.topics_to_provision


def test_refuses_an_empty_manifest_under_profile_and_manifest() -> None:
    with pytest.raises(TopicProvisionRuleError, match="non-empty lane_manifest"):
        plan_topic_provision(
            _request(EnumTopicProvisionMode.PROFILE_AND_MANIFEST, lane_manifest=())
        )


def test_refuses_a_manifest_naming_an_unknown_contract() -> None:
    with pytest.raises(TopicProvisionRuleError, match=r"not among request\.contracts"):
        plan_topic_provision(
            _request(
                EnumTopicProvisionMode.PROFILE_AND_MANIFEST,
                lane_manifest=("delegation", "ghost"),
            )
        )


def test_refuses_an_unknown_profile() -> None:
    req = _request(EnumTopicProvisionMode.PROFILE)
    req = ModelTopicProvisionRequest(
        contracts=req.contracts,
        profile="nope",
        lane_manifest=req.lane_manifest,
        live_topics=req.live_topics,
        rule=req.rule,
    )
    with pytest.raises(TopicProvisionRuleError, match="nope"):
        plan_topic_provision(req)


def test_the_plan_is_the_same_in_any_input_order() -> None:
    forward = plan_topic_provision(
        _request(EnumTopicProvisionMode.PROFILE_AND_MANIFEST)
    )
    reversed_req = ModelTopicProvisionRequest(
        contracts=tuple(reversed(_contracts())),
        profile="main",
        lane_manifest=("board", "delegation"),
        live_topics=(),
        rule=ModelTopicProvisionRule(mode=EnumTopicProvisionMode.PROFILE_AND_MANIFEST),
    )
    backward = plan_topic_provision(reversed_req)
    assert forward == backward


def test_dry_run_counts() -> None:
    plan = plan_topic_provision(
        _request(
            EnumTopicProvisionMode.PROFILE_AND_MANIFEST, live_topics=("a", "c", "z")
        )
    )
    assert plan.live_topic_count == 3
    assert plan.live_topics_under_rule == 1
    assert plan.live_topics_outside_rule == 2
    assert plan.live_topics_undeclared == 1
    assert plan.declared_topic_count == 4


def test_handle_matches_the_function() -> None:
    req = _request(EnumTopicProvisionMode.PROFILE_AND_MANIFEST)
    assert HandlerTopicProvisionPlan().handle(req) == plan_topic_provision(req)
