# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declared topics are boot provisioned for the OMN-19998 node groups.

The dev runtime's boot provisioning creates only the topics
_contract_provision_topics returns for a loaded contract: event_bus
subscribe/publish/dlq topics and projection_api topics. The Group A and B
contracts declare their command and terminal topics in a `topics:` map, in
runtime_dispatch (command_topic, terminal_events) or in terminal_event, and
keep event_bus.subscribe_topics / publish_topics empty, so the topics never
exist on the dev broker. These tests keep every declared topic boot
provisioned and keep the Group A command topics out of subscriptions.
"""

from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.runtime.auto_wiring.handler_wiring import _contract_provision_topics

from omnimarket.models.broker_acl import (
    EnumAclResourceType,
    ModelBrokerAclCoverageRequest,
    ModelBrokerAclRow,
)
from omnimarket.nodes.node_broker_acl_overlay_render_compute.handlers.handler_broker_acl_coverage_gate import (
    HandlerBrokerAclCoverageGate,
)
from omnimarket.nodes.node_broker_acl_reconcile_effect.handlers.contract_topics import (
    parse_topic_uses,
)
from omnimarket.nodes.node_broker_acl_reconcile_effect.handlers.handler_broker_acl_reconcile import (
    aggregate_declared_grants,
)

pytestmark = pytest.mark.unit

NODES: Path = Path(__file__).parents[2] / "src" / "omnimarket" / "nodes"

GROUP_A: tuple[str, ...] = (
    "node_broker_auth_refusal_reading_effect",
    "node_bus_login_exposure_effect",
    "node_commitment_check_runner_effect",
    "node_daily_line_effect",
    "node_delegation_completion_rate_effect",
    "node_delegation_events_defect_scan_effect",
    "node_order_advance_effect",
    "node_overlay_drift_effect",
    "node_stability_lane_refresh_effect",
    "node_stop_escalation_effect",
    "node_validator_workflow_orchestrator",
    "node_lab_proof_judge_compute",
)

# daily_line_effect is a scheduled writer: it declares a terminal event and no command topic.
GROUP_A_WITHOUT_COMMAND_TOPIC: tuple[str, ...] = ("node_daily_line_effect",)

GROUP_B: tuple[str, ...] = (
    "node_ask_state_compute",
    "node_delegated_test_control_compute",
    "node_delegation_readiness_verdict_compute",
    "node_delegation_scenario_days_compute",
)


def _declared_topics(contract: dict[str, Any]) -> set[str]:
    """Every topic the contract declares in its topics map, runtime_dispatch and terminal_event."""
    topics: set[str] = set()
    for value in (contract.get("topics") or {}).values():
        if isinstance(value, str):
            topics.add(value)
    dispatch = contract.get("runtime_dispatch") or {}
    command_topic = dispatch.get("command_topic")
    if isinstance(command_topic, str):
        topics.add(command_topic)
    for value in (dispatch.get("terminal_events") or {}).values():
        if isinstance(value, str):
            topics.add(value)
    terminal_event = contract.get("terminal_event")
    if isinstance(terminal_event, str):
        topics.add(terminal_event)
    return topics


def _provisioned_topics(node_dir: Path) -> set[str]:
    """The topics the dev runtime's boot provisioning creates for one node's contract."""
    manifest = discover_contracts_from_paths([node_dir / "contract.yaml"])
    provisioned: set[str] = set()
    for contract in manifest.contracts:
        provisioned.update(_contract_provision_topics(contract))
    return provisioned


def undeclared_topics(contract: dict[str, Any], provisioned: set[str]) -> list[str]:
    """Declared topics the boot provisioning does not create, sorted."""
    return sorted(
        topic for topic in _declared_topics(contract) if topic not in provisioned
    )


@pytest.mark.parametrize("node_dir", GROUP_A + GROUP_B)
def test_every_declared_topic_is_boot_provisioned(node_dir: str) -> None:
    contract = yaml.safe_load(
        (NODES / node_dir / "contract.yaml").read_text(encoding="utf-8")
    )
    provisioned = _provisioned_topics(NODES / node_dir)

    assert undeclared_topics(contract, provisioned) == []


@pytest.mark.parametrize(
    "node_dir", [n for n in GROUP_A if n not in GROUP_A_WITHOUT_COMMAND_TOPIC]
)
def test_group_a_command_topic_is_consumed_not_subscribed(node_dir: str) -> None:
    contract = yaml.safe_load(
        (NODES / node_dir / "contract.yaml").read_text(encoding="utf-8")
    )
    command_topics = [
        topic for topic in _declared_topics(contract) if topic.startswith("onex.cmd.")
    ]
    assert command_topics, f"{node_dir} declares no command topic"
    bus = contract.get("event_bus") or {}
    consumed = set(contract.get("externally_consumed_topics") or [])
    subscribed = set(bus.get("subscribe_topics") or [])

    for topic in command_topics:
        assert topic in consumed, (
            f"{node_dir}: {topic} is not in externally_consumed_topics"
        )
        assert topic not in subscribed, f"{node_dir}: {topic} must not be subscribed"


@pytest.mark.parametrize("node_dir", GROUP_B)
def test_group_b_command_topic_is_subscribed_and_profiled(node_dir: str) -> None:
    contract = yaml.safe_load(
        (NODES / node_dir / "contract.yaml").read_text(encoding="utf-8")
    )
    command_topic = (contract.get("runtime_dispatch") or {}).get("command_topic")
    assert isinstance(command_topic, str), (
        f"{node_dir} has no runtime_dispatch.command_topic"
    )
    bus = contract.get("event_bus") or {}

    assert command_topic in (bus.get("subscribe_topics") or []), (
        f"{node_dir}: {command_topic} is not in event_bus.subscribe_topics"
    )
    assert contract.get("runtime_profiles"), f"{node_dir} has no runtime_profiles"


def test_undeclared_topics_helper_golden() -> None:
    contract: dict[str, Any] = {
        "topics": {"one": "onex.cmd.example.one.v1", "two": "onex.evt.example.two.v1"},
        "runtime_dispatch": {
            "command_topic": "onex.cmd.example.three.v1",
            "terminal_events": {"success": "onex.evt.example.four.v1"},
        },
        "terminal_event": "onex.evt.example.five.v1",
    }
    provisioned = _declared_topics(contract)

    assert undeclared_topics(contract, provisioned) == []


def test_undeclared_topics_helper_error() -> None:
    contract: dict[str, Any] = {"topics": {"only": "onex.cmd.example.only.v1"}}

    assert undeclared_topics(contract, set()) == ["onex.cmd.example.only.v1"]


@pytest.mark.parametrize("node_dir", GROUP_A + GROUP_B)
def test_every_topic_use_has_a_declared_broker_grant(node_dir: str) -> None:
    """The seam with the permission coverage check (OMN-24005): a topic use the contract lists needs its declared rows."""
    text = (NODES / node_dir / "contract.yaml").read_text(encoding="utf-8")
    declared = tuple(
        ModelBrokerAclRow(**row)
        for row in aggregate_declared_grants(NODES)
        if row["resource_type"] == EnumAclResourceType.TOPIC.value
    )
    uses = parse_topic_uses(text)
    assert uses, f"{node_dir} lists no topic use in event_bus"

    result = HandlerBrokerAclCoverageGate().handle(
        ModelBrokerAclCoverageRequest(
            topic_uses=uses, base_topic_uses=(), declared_rows=declared
        )
    )

    assert result.status == "covered", [f.detail for f in result.findings]


def test_the_coverage_check_refuses_a_use_with_no_declared_grant() -> None:
    uses = parse_topic_uses(
        "name: node_example\nevent_bus:\n  publish_topics:\n    - onex.evt.example.one.v1\n"
    )

    result = HandlerBrokerAclCoverageGate().handle(
        ModelBrokerAclCoverageRequest(
            topic_uses=uses, base_topic_uses=(), declared_rows=()
        )
    )

    assert result.status == "refused"
    assert result.uncovered_new_uses == 1
