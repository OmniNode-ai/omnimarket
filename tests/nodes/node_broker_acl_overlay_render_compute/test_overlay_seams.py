# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seams of the broker permission overlay (OMN-23589).

Seam 1: the overlay's shape and the shape the broker permission apply tool reads as its declaration. The tool's key lists are
spelled here (it lives in a frozen repository this one never reads); a change on either side fails this file.
Seam 2: the two topics of the cost estimator, as the estimator's contract declares them and as the overlays
repository's overlay for them names them (its test spells the same two names).
"""

from importlib import resources

import pytest
import yaml

from omnimarket.models.broker_acl import (
    EnumAclOperation,
    EnumAclPattern,
    EnumAclResourceType,
    ModelBrokerAclGrant,
    ModelBrokerAclPrincipal,
    ModelBrokerAclScopeOverlay,
)

pytestmark = pytest.mark.unit

# What the apply tool's declaration loader reads (it lives in a frozen repository this one never reads).
APPLY_TOOL_TOP_KEYS = {
    "lane",
    "host",
    "compose_project",
    "broker_container",
    "superuser_source_container",
    "principals",
}
APPLY_TOOL_GRANT_KEYS = {"resource_type", "pattern", "name", "operation"}
APPLY_TOOL_OPERATIONS = {
    "READ",
    "WRITE",
    "DESCRIBE",
    "DELETE",
    "IDEMPOTENT_WRITE",
    "CREATE",
    "DESCRIBE_CONFIGS",
}
APPLY_TOOL_RESOURCE_TYPES = {"TOPIC", "GROUP", "CLUSTER"}
APPLY_TOOL_PATTERNS = {"LITERAL", "PREFIXED"}

ESTIMATE_TOPICS = (
    "onex.cmd.omnimarket.prompt-cost-estimate-requested.v1",
    "onex.evt.omnimarket.prompt-cost-estimate-completed.v1",
)


def test_overlay_carries_every_key_the_apply_tool_reads_and_adds_only_its_own_cells() -> (
    None
):
    fields = set(ModelBrokerAclScopeOverlay.model_fields)
    assert fields >= APPLY_TOOL_TOP_KEYS
    assert fields - APPLY_TOOL_TOP_KEYS == {
        "scope",
        "ticket",
        "why",
        "operator_ssh_target",
        "operator_command",
        "declaration_path",
    }
    assert set(ModelBrokerAclGrant.model_fields) == APPLY_TOOL_GRANT_KEYS
    assert "principal" in ModelBrokerAclPrincipal.model_fields


def test_enums_are_exactly_the_apply_tools_vocabulary() -> None:
    assert {o.value for o in EnumAclOperation} == APPLY_TOOL_OPERATIONS
    assert {r.value for r in EnumAclResourceType} == APPLY_TOOL_RESOURCE_TYPES
    assert {p.value for p in EnumAclPattern} == APPLY_TOOL_PATTERNS


def test_the_example_overlay_loads_through_the_model() -> None:
    text = (
        resources.files("omnimarket.nodes.node_broker_acl_overlay_render_compute")
        .joinpath("overlay.example.yaml")
        .read_text(encoding="utf-8")
    )
    overlay = ModelBrokerAclScopeOverlay.model_validate(yaml.safe_load(text))
    assert overlay.principals[0].grants[0].operation is EnumAclOperation.READ


def test_estimator_contract_topics_are_the_ones_the_overlay_grants() -> None:
    text = (
        resources.files("omnimarket.nodes.node_prompt_cost_estimate_compute")
        .joinpath("contract.yaml")
        .read_text(encoding="utf-8")
    )
    contract = yaml.safe_load(text)
    assert (
        contract["runtime_dispatch"]["command_topic"],
        contract["terminal_event"],
    ) == ESTIMATE_TOPICS
