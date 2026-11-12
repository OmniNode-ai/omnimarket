# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay render node refuses every row the apply tool would refuse and renders nothing for it (OMN-23589)."""

import pytest
from pydantic import ValidationError

from omnimarket.models.broker_acl import (
    EnumBrokerAclFindingCode,
    ModelBrokerAclRenderRequest,
    ModelBrokerAclScopeOverlay,
)
from omnimarket.nodes.node_broker_acl_overlay_render_compute.handlers.handler_broker_acl_overlay_render import (
    HandlerBrokerAclOverlayRender,
)

pytestmark = pytest.mark.unit

_CODES = EnumBrokerAclFindingCode


def _grant(**changes):
    grant = {
        "resource_type": "TOPIC",
        "pattern": "LITERAL",
        "name": "onex.evt.omnimarket.example.v1",
        "operation": "READ",
    }
    grant.update(changes)
    return grant


def _principal(principal="User:lab-login", grants=None):
    return {"principal": principal, "role": "lab", "grants": grants or (_grant(),)}


def _overlay(**changes):
    doc = {
        "scope": "broker_acls_example",
        "ticket": "OMN-23589",
        "why": "one example grant",
        "operator_ssh_target": "broker",
        "operator_command": "apply-tool {declaration} {ssh_target}",
        "declaration_path": "{scope}.yaml",
        "lane": "dev",
        "host": "broker-host",
        "compose_project": "omni",
        "broker_container": "kafka",
        "superuser_source_container": "admin",
        "principals": changes.pop("principals", (_principal(),)),
    }
    doc.update(changes)
    return ModelBrokerAclScopeOverlay.model_validate(doc)


def _result(principal="User:lab-login", grants=None, scope="broker_acls_example"):
    return HandlerBrokerAclOverlayRender().handle(
        ModelBrokerAclRenderRequest(
            scope=scope, overlay=_overlay(principals=(_principal(principal, grants),))
        )
    )


def _refusal(**changes):
    result = _result(**changes)
    assert (result.status, result.rows, result.operator_apply) == ("refused", (), "")
    return [finding.code for finding in result.findings]


@pytest.mark.parametrize("principal", ["User:*", "bare"])
def test_a_principal_that_is_not_a_concrete_user_is_refused(principal):
    assert _refusal(principal=principal) == [_CODES.PRINCIPAL_INVALID]


def test_a_wildcard_resource_name_is_refused():
    assert _refusal(grants=(_grant(name="*"),)) == [_CODES.RESOURCE_NAME_INVALID]


def test_delete_on_a_topic_is_refused():
    assert _refusal(grants=(_grant(operation="DELETE"),)) == [
        _CODES.DELETE_NOT_ON_GROUP
    ]


def test_delete_on_a_prefixed_group_is_rendered():
    delete = _grant(
        resource_type="GROUP",
        pattern="PREFIXED",
        name="example-group.",
        operation="DELETE",
    )
    result = _result(grants=(delete,))
    assert result.status == "rendered"
    assert (result.declared, len(result.rows)) == (1, 1)
    assert "--dry-run" in result.operator_dry_run


@pytest.mark.parametrize(
    "grant",
    [
        _grant(resource_type="CLUSTER", name="other"),
        _grant(resource_type="CLUSTER", pattern="PREFIXED", name="kafka-cluster"),
    ],
)
def test_a_cluster_grant_off_the_literal_cluster_is_refused(grant):
    assert _refusal(grants=(grant,)) == [_CODES.CLUSTER_NOT_LITERAL]


def test_a_duplicated_grant_is_refused():
    assert _refusal(grants=(_grant(), _grant())) == [_CODES.DUPLICATE_GRANT]


@pytest.mark.parametrize(
    "grant",
    [
        _grant(name="onex.cmd.omnimarket.github-repo-write-requested.v1"),
        _grant(
            name="onex.evt.omnimarket.github-repo-write-completed.v1",
            operation="WRITE",
        ),
        _grant(pattern="PREFIXED", name="onex.cmd.omnimarket.github-repo"),
    ],
)
def test_a_grant_into_the_write_node_s_topics_is_refused(grant):
    assert _refusal(grants=(grant,)) == [_CODES.FORBIDDEN_SCM_GRANT]


def test_a_scope_that_differs_from_the_overlay_is_refused():
    assert _refusal(scope="other_scope") == [_CODES.SCOPE_MISMATCH]


def test_a_structurally_bad_document_does_not_validate():
    with pytest.raises(ValidationError):
        _overlay(extra_cell="nope")
    doc = _overlay().model_dump(mode="json")
    doc["principals"][0]["grants"][0]["operation"] = "SUDO"
    with pytest.raises(ValidationError):
        ModelBrokerAclScopeOverlay.model_validate(doc)
