# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A topic is planned only for a contract a running profile loads (OMN-23614)."""

from pathlib import Path

import pytest
import yaml

from omnimarket.handlers.node_overlay_reader import NodeOverlayNotFoundError
from omnimarket.nodes.node_topic_provision_plan_compute import (
    NodeTopicProvisionPlanCompute,
)
from omnimarket.nodes.node_topic_provision_plan_compute.handlers.handler_topic_provision_plan import (
    HandlerTopicProvisionPlan,
)
from omnimarket.nodes.node_topic_provision_plan_compute.models.model_topic_provision_plan import (
    ModelContractTopics,
    ModelLiveTopic,
    ModelTopicProvisionPlanRequest,
    ModelTopicProvisionRule,
)
from omnimarket.nodes.node_topic_provision_plan_compute.overlay import (
    NODE_NAME,
    load_provision_rule,
)

pytestmark = pytest.mark.unit

NODE_DIR = Path(__file__).resolve().parents[3] / "src/omnimarket/nodes" / NODE_NAME


def _rule(**over: object) -> ModelTopicProvisionRule:
    values: dict[str, object] = {
        "include_subscribe_topics": True,
        "include_publish_topics": True,
        "default_partitions": 2,
        "partition_bar": 10,
    }
    values.update(over)
    return ModelTopicProvisionRule(**values)


CONTRACTS = (
    ModelContractTopics(
        contract_name="a", subscribe_topics=("t.a.cmd",), publish_topics=("t.a.evt",)
    ),
    ModelContractTopics(
        contract_name="b", subscribe_topics=("t.a.evt",), publish_topics=("t.b.evt",)
    ),
    ModelContractTopics(
        contract_name="c", subscribe_topics=("t.c.cmd",), publish_topics=("t.c.evt",)
    ),
)
LIVE = (
    ModelLiveTopic(name="t.a.cmd", partitions=1),
    ModelLiveTopic(name="t.c.cmd", partitions=3),
    ModelLiveTopic(name="t.stray", partitions=4),
)


def _plan(loaded: tuple[str, ...], rule: ModelTopicProvisionRule | None = None):
    return NodeTopicProvisionPlanCompute().handle(
        ModelTopicProvisionPlanRequest(
            rule=rule or _rule(),
            contracts=CONTRACTS,
            loaded_contract_names=loaded,
            live_topics=LIVE,
        )
    )


def test_a_contract_outside_the_profile_gets_no_topic() -> None:
    plan = _plan(("a", "b"))
    assert plan.status == "planned"
    assert plan.topics_to_provision == ("t.a.cmd", "t.a.evt", "t.b.evt")
    assert "t.c.cmd" not in plan.topics_to_provision
    assert "t.c.evt" not in plan.topics_to_provision
    assert plan.excluded_contracts == ("c",)
    assert (plan.contracts_total, plan.contracts_loaded) == (3, 2)
    assert plan.live_topics_total == 3
    assert plan.live_topics_inside_rule == 1  # t.a.cmd
    assert plan.live_topics_outside_rule == 2  # t.c.cmd and t.stray
    assert plan.topics_to_create == 2  # t.a.evt and t.b.evt
    assert plan.would_exist_topics == 3
    assert plan.projected_partitions == 1 + 2 * 2
    assert plan.partition_bar == 10
    assert plan.under_partition_bar is True


def test_failing_control_every_contract_loaded_provisions_the_third_one_too() -> None:
    """Without the loaded-contract rule the outside contract's topics are planned."""
    plan = _plan(("a", "b", "c"))
    assert "t.c.evt" in plan.topics_to_provision
    assert plan.excluded_contracts == ()
    assert plan.live_topics_outside_rule == 1  # only t.stray


def test_an_empty_profile_is_refused_and_plans_nothing() -> None:
    plan = _plan(())
    assert plan.status == "refused"
    assert plan.refusal_reasons == ("no_loaded_contracts",)
    assert plan.topics_to_provision == ()


def test_a_loaded_name_no_contract_holds_is_refused_by_name() -> None:
    plan = _plan(("a", "ghost", "alien"))
    assert plan.status == "refused"
    assert plan.refusal_reasons == (
        "unknown_loaded_contract:alien",
        "unknown_loaded_contract:ghost",
    )
    assert plan.topics_to_provision == ()


def test_the_partition_bar_is_held_on_both_sides() -> None:
    assert _plan(("a", "b"), _rule(partition_bar=5)).under_partition_bar is True
    over = _plan(("a", "b"), _rule(partition_bar=4))
    assert over.projected_partitions == 5
    assert over.under_partition_bar is False


def test_the_rule_flags_decide_which_topic_kinds_count() -> None:
    plan = _plan(("a", "b"), _rule(include_publish_topics=False))
    assert plan.topics_to_provision == ("t.a.cmd", "t.a.evt")  # b subscribes t.a.evt


def test_the_rule_is_read_only_from_the_overlay(tmp_path: Path) -> None:
    root = tmp_path / NODE_NAME
    root.mkdir()
    (root / "overlay.yaml").write_text(
        (NODE_DIR / "overlay.example.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    rule = load_provision_rule({"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)})
    assert rule.partition_bar > 0
    with pytest.raises(NodeOverlayNotFoundError):
        load_provision_rule({"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path / "none")})


def test_the_contract_declares_exactly_the_command_and_the_completed_event() -> None:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text(encoding="utf-8"))
    assert contract["event_bus"]["subscribe_topics"] == [
        "onex.cmd.omnimarket.topic-provision-plan-requested.v1"
    ]
    assert contract["event_bus"]["publish_topics"] == [
        "onex.evt.omnimarket.topic-provision-plan-completed.v1"
    ]


def test_the_node_answers_as_its_handler_does() -> None:
    request = ModelTopicProvisionPlanRequest(
        rule=_rule(),
        contracts=CONTRACTS,
        loaded_contract_names=("a",),
        live_topics=LIVE,
    )
    assert NodeTopicProvisionPlanCompute().handle(
        request
    ) == HandlerTopicProvisionPlan().handle(request)
