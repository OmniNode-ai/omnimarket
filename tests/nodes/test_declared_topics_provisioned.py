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


# The topics each listed contract's event_bus publishes and subscribes, pinned by name: the seam with the boot
# provisioning and the permission check, which fails when either side renames or drops a topic (OMN-24070).
EXPECTED_PUBLISH_TOPICS: dict[str, tuple[str, ...]] = {
    "node_broker_auth_refusal_reading_effect": (
        "onex.cmd.omnimarket.broker-auth-refusal-requested.v1",
        "onex.evt.omnimarket.broker-auth-refusal-written.v1",
        "onex.evt.omnimarket.broker-auth-refusal-failed.v1",
        "onex.evt.omnimarket.broker-auth-refusal-run-completed.v1",
        "onex.evt.omnimarket.broker-auth-refusal-run-failed.v1",
    ),
    "node_bus_login_exposure_effect": (
        "onex.cmd.omnimarket.bus-login-exposure-probe-requested.v1",
        "onex.evt.omnimarket.bus-login-exposure-probed.v1",
        "onex.evt.omnimarket.bus-login-exposure-probe-failed.v1",
    ),
    "node_commitment_check_runner_effect": (
        "onex.cmd.omnimarket.commitment-check-requested.v1",
        "onex.evt.omnimarket.commitment-verified.v1",
        "onex.evt.omnimarket.commitment-regressed.v1",
        "onex.evt.omnimarket.commitment-check-failed.v1",
        "onex.evt.omnimarket.commitment-check-completed.v1",
        "onex.evt.omnimarket.commitment-check-run-failed.v1",
    ),
    "node_daily_line_effect": ("onex.evt.omnimarket.daily-line-published.v1",),
    "node_delegation_completion_rate_effect": (
        "onex.cmd.omnimarket.delegation-completion-rate-run-requested.v1",
        "onex.evt.omnimarket.delegation-completion-rate-run-started.v1",
        "onex.evt.omnimarket.delegation-completion-rate-run-completed.v1",
        "onex.evt.omnimarket.delegation-completion-rate-run-failed.v1",
    ),
    "node_delegation_events_defect_scan_effect": (
        "onex.cmd.omnimarket.delegation-events-defect-scan-run-requested.v1",
        "onex.evt.omnimarket.delegation-events-defect-scan-run-started.v1",
        "onex.evt.omnimarket.delegation-events-defect-scan-run-completed.v1",
        "onex.evt.omnimarket.delegation-events-defect-scan-run-failed.v1",
    ),
    "node_order_advance_effect": (
        "onex.cmd.omnimarket.order-advance-requested.v1",
        "onex.evt.omnimarket.order-advance-answered.v1",
        "onex.evt.omnimarket.order-advance-failed.v1",
    ),
    "node_overlay_drift_effect": (
        "onex.cmd.omnimarket.overlay-drift-requested.v1",
        "onex.evt.omnimarket.overlay-drift-written.v1",
        "onex.evt.omnimarket.overlay-drift-failed.v1",
        "onex.evt.omnimarket.overlay-drift-run-completed.v1",
        "onex.evt.omnimarket.overlay-drift-run-failed.v1",
    ),
    "node_stability_lane_refresh_effect": (
        "onex.cmd.omnimarket.stability-refresh-requested.v1",
        "onex.evt.omnimarket.stability-refresh-pin-accepted.v1",
        "onex.evt.omnimarket.stability-refresh-refused.v1",
        "onex.evt.omnimarket.stability-refresh-completed.v1",
        "onex.evt.omnimarket.stability-refresh-failed.v1",
    ),
    "node_stop_escalation_effect": (
        "onex.cmd.omnimarket.stop-escalation-requested.v1",
        "onex.evt.omnimarket.stop-escalated.v1",
    ),
    "node_validator_workflow_orchestrator": (
        "onex.cmd.omnimarket.validator-workflow-requested.v1",
        "onex.evt.omnimarket.validator-workflow-started.v1",
        "onex.evt.omnimarket.validator-workflow-validator-skipped.v1",
        "onex.evt.omnimarket.validator-workflow-validator-completed.v1",
        "onex.evt.omnimarket.validator-workflow-ratchet-refused.v1",
        "onex.evt.omnimarket.validator-workflow-verdict-folded.v1",
        "onex.evt.omnimarket.validator-workflow-completed.v1",
        "onex.evt.omnimarket.validator-workflow-failed.v1",
    ),
    "node_lab_proof_judge_compute": (
        "onex.cmd.omnimarket.lab-proof-judge-requested.v1",
        "onex.evt.omnimarket.lab-proof-judge-decided.v1",
        "onex.evt.omnimarket.lab-proof-judge-run-failed.v1",
    ),
    "node_ask_state_compute": (
        "onex.evt.omnimarket.ask-state-completed.v1",
        "onex.evt.omnimarket.ask-state-failed.v1",
    ),
    "node_delegated_test_control_compute": (
        "onex.evt.omnimarket.delegated-test-control-completed.v1",
        "onex.evt.omnimarket.delegated-test-control-failed.v1",
    ),
    "node_delegation_readiness_verdict_compute": (
        "onex.evt.omnimarket.delegation-readiness-verdict-computed.v1",
    ),
    "node_delegation_scenario_days_compute": (
        "onex.evt.omnimarket.delegation-scenario-green-days-computed.v1",
    ),
}

EXPECTED_SUBSCRIBE_TOPICS: dict[str, tuple[str, ...]] = {
    "node_ask_state_compute": ("onex.cmd.omnimarket.ask-state-requested.v1",),
    "node_delegated_test_control_compute": (
        "onex.cmd.omnimarket.delegated-test-control-requested.v1",
    ),
    "node_delegation_readiness_verdict_compute": (
        "onex.cmd.omnimarket.delegation-readiness-verdict-compute-requested.v1",
    ),
    "node_delegation_scenario_days_compute": (
        "onex.cmd.omnimarket.delegation-scenario-green-days-compute-requested.v1",
    ),
}


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


@pytest.mark.parametrize("node_dir", GROUP_A + GROUP_B)
def test_event_bus_publish_topics_are_the_pinned_set(node_dir: str) -> None:
    contract = yaml.safe_load(
        (NODES / node_dir / "contract.yaml").read_text(encoding="utf-8")
    )

    published = tuple((contract.get("event_bus") or {}).get("publish_topics") or [])

    assert published == EXPECTED_PUBLISH_TOPICS[node_dir]


@pytest.mark.parametrize("node_dir", GROUP_A + GROUP_B)
def test_event_bus_subscribe_topics_are_the_pinned_set(node_dir: str) -> None:
    contract = yaml.safe_load(
        (NODES / node_dir / "contract.yaml").read_text(encoding="utf-8")
    )

    subscribed = tuple((contract.get("event_bus") or {}).get("subscribe_topics") or [])

    assert subscribed == EXPECTED_SUBSCRIBE_TOPICS.get(node_dir, ())
