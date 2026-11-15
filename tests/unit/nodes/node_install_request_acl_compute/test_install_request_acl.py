# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for node_install_request_acl_compute (OMN-24546)."""

from __future__ import annotations

import pytest

from omnimarket.models.broker_acl.model_broker_acl_overlay import (
    EnumAclOperation,
    EnumAclPattern,
    EnumAclResourceType,
    ModelBrokerAclGrant,
    ModelBrokerAclPrincipal,
    ModelBrokerAclRenderRequest,
    ModelBrokerAclScopeOverlay,
)
from omnimarket.models.install_request import EnumInstallTarget
from omnimarket.nodes.node_broker_acl_overlay_render_compute.handlers.handler_broker_acl_overlay_render import (
    HandlerBrokerAclOverlayRender,
)
from omnimarket.nodes.node_install_request_acl_compute.handlers.handler_install_request_acl import (
    HandlerInstallRequestAcl,
    permits,
)
from omnimarket.nodes.node_install_request_acl_compute.models.model_install_request_acl import (
    ModelInstallAclRequest,
    ModelInstallAclScopeCells,
)
from omnimarket.nodes.node_install_request_effect.overlay import (
    ModelInstallHostRow,
    ModelInstallRepositoryRow,
    ModelInstallRequestEffectOverlay,
    ModelInstallUnitRow,
    ModelInstallVerifyRow,
)
from omnimarket.nodes.node_install_request_effect.runtime_install_request import (
    ModelInstallRequestTopics,
    host_group_id,
    load_install_request_topics,
)

pytestmark = pytest.mark.unit


def _cells() -> ModelInstallAclScopeCells:
    return ModelInstallAclScopeCells(
        scope="install-request",
        ticket="OMN-24546",
        why="derive broker ACL for install request effect",
        operator_ssh_target="operator@host",
        operator_command="kubectl exec broker -- kafka-acls.sh",
        declaration_path="/tmp/decl.yaml",
        lane="install",
        host="host-1",
        compose_project="omnimarket",
        broker_container="broker",
        superuser_source_container="superuser",
    )


def _topics() -> ModelInstallRequestTopics:
    """The node's own contract topics: a test that spelled its own would pass whatever the contract says."""
    return load_install_request_topics()


def _unit() -> ModelInstallUnitRow:
    repo = ModelInstallRepositoryRow(
        name="owner/repo",
        mirror_path="/tmp/mirror",
        remote_ref_prefix="origin/",
        landing_branch="main",
    )
    verify = ModelInstallVerifyRow(
        name="check",
        argv=("/bin/true",),
        expected_line="ok",
    )
    return ModelInstallUnitRow(
        name="unit-1",
        kind=EnumInstallTarget.UNIT_TREE,
        repositories=(repo,),
        verifies=(verify,),
    )


def _overlay() -> ModelInstallRequestEffectOverlay:
    host1 = ModelInstallHostRow(
        name="host-1",
        allowed_publishers=("logina", "loginb"),
        host_principal="host1",
        units=(_unit(),),
    )
    host2 = ModelInstallHostRow(
        name="host-2",
        allowed_publishers=("loginb", "loginc"),
        host_principal="host2",
        units=(_unit(),),
    )
    return ModelInstallRequestEffectOverlay(
        host_name_env="ONEX_HOST",
        default_expiry_minutes=30,
        command_timeout_seconds=120,
        hold_retry_seconds=10,
        max_command_age_seconds=3600,
        output_max_chars=4096,
        journal_dir_env="ONEX_JOURNAL_DIR",
        journal_file="journal.jsonl",
        publisher_login_env="ONEX_PUBLISHER_LOGIN",
        owed_quiet_seconds=5,
        git_command=("/usr/bin/git",),
        hosts=(host1, host2),
    )


def _request() -> ModelInstallAclRequest:
    return ModelInstallAclRequest(
        cells=_cells(),
        overlay=_overlay(),
        topics=_topics(),
        group_prefix=host_group_id(""),
    )


def _handler() -> HandlerInstallRequestAcl:
    return HandlerInstallRequestAcl()


def test_golden_two_hosts_two_publishers():
    """Each publisher principal holds WRITE+DESCRIBE on exactly its own writer topic; host principal holds READ on both allowed logins' topics and WRITE on its own result topic."""
    overlay = _handler().handle(_request())
    principals = {p.principal: p for p in overlay.principals}

    # logina is a publisher only
    pa = principals["User:logina"]
    a_grants = {(g.resource_type, g.name, g.operation) for g in pa.grants}
    assert (
        EnumAclResourceType.TOPIC,
        "onex.cmd.omnimarket.install-requested-from-logina.v1",
        EnumAclOperation.WRITE,
    ) in a_grants
    assert (
        EnumAclResourceType.TOPIC,
        "onex.cmd.omnimarket.install-requested-from-logina.v1",
        EnumAclOperation.DESCRIBE,
    ) in a_grants
    assert len(a_grants) == 4

    # loginc is a publisher only
    pc = principals["User:loginc"]
    c_grants = {(g.resource_type, g.name, g.operation) for g in pc.grants}
    assert (
        EnumAclResourceType.TOPIC,
        "onex.cmd.omnimarket.install-requested-from-loginc.v1",
        EnumAclOperation.WRITE,
    ) in c_grants
    assert (
        EnumAclResourceType.TOPIC,
        "onex.cmd.omnimarket.install-requested-from-loginc.v1",
        EnumAclOperation.DESCRIBE,
    ) in c_grants
    assert len(c_grants) == 4

    # host1 is a host principal; its allowed logins are logina and loginb
    ph1 = principals["User:host1"]
    h1_grants = {(g.resource_type, g.name, g.operation) for g in ph1.grants}
    assert (
        EnumAclResourceType.TOPIC,
        "onex.cmd.omnimarket.install-requested-from-logina.v1",
        EnumAclOperation.READ,
    ) in h1_grants
    assert (
        EnumAclResourceType.TOPIC,
        "onex.cmd.omnimarket.install-requested-from-loginb.v1",
        EnumAclOperation.READ,
    ) in h1_grants
    assert (
        EnumAclResourceType.TOPIC,
        "onex.evt.omnimarket.install-request-completed-from-host1.v1",
        EnumAclOperation.WRITE,
    ) in h1_grants
    assert (
        EnumAclResourceType.TOPIC,
        "onex.evt.omnimarket.install-request-completed-from-host1.v1",
        EnumAclOperation.DESCRIBE,
    ) in h1_grants
    assert (
        EnumAclResourceType.TOPIC,
        "onex.evt.omnimarket.install-request-completed.v1",
        EnumAclOperation.WRITE,
    ) in h1_grants
    assert (
        EnumAclResourceType.TOPIC,
        "onex.evt.omnimarket.install-request-failed.v1",
        EnumAclOperation.WRITE,
    ) in h1_grants
    assert (
        EnumAclResourceType.GROUP,
        host_group_id(""),
        EnumAclOperation.READ,
    ) in h1_grants
    assert (
        EnumAclResourceType.GROUP,
        host_group_id(""),
        EnumAclOperation.DESCRIBE,
    ) in h1_grants


def test_error_chain_forgery():
    """permits is False for login B writing login A's writer topic, True for A writing its own; False for host-1's principal writing host-2's result topic; False for any publisher writing the shared success topic."""
    overlay = _handler().handle(_request())
    topics = _topics()

    # loginb cannot write logina's writer topic
    assert (
        permits(
            overlay,
            "loginb",
            EnumAclResourceType.TOPIC,
            topics.writer_command_topic("logina"),
            EnumAclOperation.WRITE,
        )
        is False
    )
    # logina can write its own writer topic
    assert (
        permits(
            overlay,
            "logina",
            EnumAclResourceType.TOPIC,
            topics.writer_command_topic("logina"),
            EnumAclOperation.WRITE,
        )
        is True
    )
    # host1 cannot write host2's result topic
    assert (
        permits(
            overlay,
            "host1",
            EnumAclResourceType.TOPIC,
            topics.result_topic("host2"),
            EnumAclOperation.WRITE,
        )
        is False
    )
    # no publisher can write the shared success topic
    assert (
        permits(
            overlay,
            "logina",
            EnumAclResourceType.TOPIC,
            topics.success,
            EnumAclOperation.WRITE,
        )
        is False
    )
    assert (
        permits(
            overlay,
            "loginb",
            EnumAclResourceType.TOPIC,
            topics.success,
            EnumAclOperation.WRITE,
        )
        is False
    )
    assert (
        permits(
            overlay,
            "loginc",
            EnumAclResourceType.TOPIC,
            topics.success,
            EnumAclOperation.WRITE,
        )
        is False
    )


def test_render_passes():
    """The output passes the broker ACL overlay render handler."""
    overlay = _handler().handle(_request())
    render = HandlerBrokerAclOverlayRender()
    result = render.handle(
        ModelBrokerAclRenderRequest(scope=_cells().scope, overlay=overlay)
    )
    assert result.status == "rendered"
    total_grants = sum(len(p.grants) for p in overlay.principals)
    assert result.declared == total_grants


def test_deterministic_same_input_same_output():
    """Same input gives same output; a login listed under two hosts appears once."""
    handler = _handler()
    req = _request()
    o1 = handler.handle(req)
    o2 = handler.handle(req)
    assert o1 == o2

    # loginb is allowed by both host1 and host2; it appears as one principal
    logins = [p.principal for p in o1.principals]
    assert logins.count("User:loginb") == 1


def test_failing_control_widened_prefix():
    """A local copy of the derivation that grants PREFIXED WRITE on the topic prefix lets permits say True for the forger."""
    overlay = _handler().handle(_request())
    topics = _topics()

    # Build a widened overlay by hand: add a PREFIXED WRITE grant on the writer topic prefix for loginb
    widened_principals = []
    for p in overlay.principals:
        if p.principal == "User:loginb":
            extra_grant = ModelBrokerAclGrant(
                resource_type=EnumAclResourceType.TOPIC,
                pattern=EnumAclPattern.PREFIXED,
                name="onex.cmd.omnimarket.install-requested-from-login",
                operation=EnumAclOperation.WRITE,
            )
            widened_principals.append(
                ModelBrokerAclPrincipal(
                    principal=p.principal,
                    role=p.role,
                    grants=(*p.grants, extra_grant),
                )
            )
        else:
            widened_principals.append(p)

    widened = ModelBrokerAclScopeOverlay(
        scope=overlay.scope,
        ticket=overlay.ticket,
        why=overlay.why,
        operator_ssh_target=overlay.operator_ssh_target,
        operator_command=overlay.operator_command,
        declaration_path=overlay.declaration_path,
        lane=overlay.lane,
        host=overlay.host,
        compose_project=overlay.compose_project,
        broker_container=overlay.broker_container,
        superuser_source_container=overlay.superuser_source_container,
        principals=tuple(widened_principals),
    )

    # The forger (loginb) can now write logina's writer topic via the PREFIXED grant
    assert (
        permits(
            widened,
            "loginb",
            EnumAclResourceType.TOPIC,
            topics.writer_command_topic("logina"),
            EnumAclOperation.WRITE,
        )
        is True
    )
    # But the real derivation refuses it
    assert (
        permits(
            overlay,
            "loginb",
            EnumAclResourceType.TOPIC,
            topics.writer_command_topic("logina"),
            EnumAclOperation.WRITE,
        )
        is False
    )
