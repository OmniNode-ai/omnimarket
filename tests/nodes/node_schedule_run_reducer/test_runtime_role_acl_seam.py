# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The dev runtime's broker login runs the schedule nodes: the grant block each contract declares and the rows the principals overlay's runtime role derives must agree (OMN-24029).

Seam: the schedule nodes' contracts (the real src/omnimarket/nodes tree) -> HandlerBrokerAclDerive, with a
ModelBrokerAclPrincipalsOverlay holding one role named runtime_schedule (principals overlay, overlays repository, pull request
pending) whose principals is an example login (the real one is the dev runtime's, in the overlays repository) and whose nodes are those. The merge gate reads the contract grant block;
the apply reads the derivation; a topic added to one side only fails here.
Exemplar followed: tests/nodes/node_broker_acl_overlay_render_compute/test_derive_roles.py (the same request builder).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.models.broker_acl import (
    EnumAclOperation,
    EnumAclPattern,
    EnumAclResourceType,
    EnumBrokerAclFindingCode,
    ModelBrokerAclDeriveRequest,
    ModelBrokerAclDeriveResult,
    ModelBrokerAclPrincipalsOverlay,
)
from omnimarket.nodes.node_broker_acl_overlay_render_compute.handlers.handler_broker_acl_derive import (
    HandlerBrokerAclDerive,
)
from omnimarket.nodes.node_broker_acl_reconcile_effect.handlers.contract_topics import (
    load_topic_uses,
)

pytestmark = pytest.mark.unit

_LOGIN = "User:example-runtime-login"
_PLACEHOLDER = "${ONEX_ACL_RUNTIME_PRINCIPAL}"
_ROOT = Path(__file__).resolve().parents[3]
_SCHEDULE_CORE = (
    "node_schedule_run_reducer",
    "node_schedule_verdict_compute",
    "node_schedule_verdict_effect",
)


def _scheduled_job_nodes() -> tuple[str, ...]:
    """Every scheduled job node whose contract grants the runtime login: found in the tree, so a new job is covered the day it lands."""
    found: list[str] = []
    for contract in sorted(
        (_ROOT / "src/omnimarket/nodes").glob("node_*_scheduled_effect/contract.yaml")
    ):
        grants = yaml.safe_load(contract.read_text(encoding="utf-8")).get(
            "broker_acl_grants", []
        )
        if any(block.get("principal") == _PLACEHOLDER for block in grants):
            found.append(contract.parent.name)
    return tuple(found)


_NODES = (*_SCHEDULE_CORE, *_scheduled_job_nodes())


def _overlay(nodes: list[str]) -> ModelBrokerAclPrincipalsOverlay:
    return ModelBrokerAclPrincipalsOverlay.model_validate(
        {
            "scope": "broker_acl_principals",
            "ticket": "OMN-19998",
            "why": "the dev runtime's broker login runs the schedule nodes",
            "roles": [
                {
                    "name": "runtime_schedule",
                    "role": "the dev runtime's broker login",
                    "nodes": nodes,
                    "principals": [_LOGIN],
                }
            ],
            "principals": [],
        }
    )


def _derive(
    overlay: ModelBrokerAclPrincipalsOverlay, only_node: str | None = None
) -> ModelBrokerAclDeriveResult:
    return HandlerBrokerAclDerive().handle(
        ModelBrokerAclDeriveRequest(
            scope="broker_acl_principals",
            topic_uses=load_topic_uses(_ROOT / "src/omnimarket/nodes"),
            principals=overlay,
            only_node=only_node,
        )
    )


def test_the_job_nodes_include_the_ones_this_seam_names() -> None:
    """Positive control: the tree walk finds the lab alarm's broker lag job, the lab state alarm, the floor fill, the commitment check and the lane result sweep jobs, so an empty walk cannot pass."""
    assert {
        "node_lab_alarm_scheduled_effect",
        "node_lab_state_alarm_scheduled_effect",
        "node_floor_fill_scheduled_effect",
        "node_commitment_check_scheduled_effect",
        "node_lane_result_sweep_scheduled_effect",
    } <= set(_scheduled_job_nodes())


def test_the_runtime_role_derives_every_topic_of_the_schedule_nodes() -> None:
    """Per node: derived with no finding, every declared topic covered by a row of the login (publish WRITE and DESCRIBE,
    subscribe READ and DESCRIBE), and a GROUP PREFIXED row for each node that subscribes."""
    uses = load_topic_uses(_ROOT / "src/omnimarket/nodes")
    for node in _NODES:
        result = _derive(_overlay(list(_NODES)), only_node=node)
        assert result.status == "derived", f"{node}: {result.findings}"
        assert result.findings == ()
        mine = {
            (r.resource_type, r.pattern, r.name, r.operation)
            for r in result.rows
            if r.principal == _LOGIN
        }
        declared = [use for use in uses if use.node == node]
        assert declared, node
        for use in declared:
            operations = (
                (EnumAclOperation.WRITE, EnumAclOperation.DESCRIBE)
                if use.direction.value == "publish"
                else (EnumAclOperation.READ, EnumAclOperation.DESCRIBE)
            )
            for operation in operations:
                assert (
                    EnumAclResourceType.TOPIC,
                    EnumAclPattern.LITERAL,
                    use.topic,
                    operation,
                ) in mine, f"{node}: {use.direction.value} {use.topic} {operation}"
        if any(use.direction.value == "subscribe" for use in declared):
            assert any(
                r.resource_type == EnumAclResourceType.GROUP
                and r.pattern == EnumAclPattern.PREFIXED
                and node in r.name
                for r in result.rows
                if r.principal == _LOGIN
            ), node


def test_each_contract_grant_block_equals_the_rows_the_role_derives() -> None:
    """The grant block each contract declares (the merge gate reads it) names the one placeholder principal
    ${ONEX_ACL_RUNTIME_PRINCIPAL} (the apply fills it from its environment; the public-repo hygiene gate refuses a concrete login in a
    contract) and its topic rows equal the rows the role derives."""
    for node in _NODES:
        loaded = yaml.safe_load(
            (_ROOT / "src/omnimarket/nodes" / node / "contract.yaml").read_text(
                encoding="utf-8"
            )
        )
        blocks = loaded["broker_acl_grants"]
        assert [block["principal"] for block in blocks] == [_PLACEHOLDER], node
        declared = {
            (g["resource_type"], g["name"], g["operation"])
            for block in blocks
            for g in block["grants"]
        }
        derived = {
            (row.resource_type.value, row.name, row.operation.value)
            for row in _derive(_overlay(list(_NODES)), only_node=node).rows
            if row.resource_type == EnumAclResourceType.TOPIC
        }
        assert declared == derived, node


def test_a_node_missing_from_the_tree_is_refused() -> None:
    """Failing control: the same overlay with one extra node that has no contract refuses with NODE_WITHOUT_CONTRACT."""
    result = _derive(_overlay([*_NODES, "node_schedule_not_built_effect"]))
    assert result.status == "refused"
    assert EnumBrokerAclFindingCode.NODE_WITHOUT_CONTRACT in {
        f.code for f in result.findings
    }
