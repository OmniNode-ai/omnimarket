# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerInstallRequestAcl: the broker permission declaration an install request effect needs (OMN-24546).

Pure definition-B compute: ``handle(request: ModelInstallAclRequest) -> ModelBrokerAclScopeOverlay``. No clock, no I/O. The same input gives the same overlay.

One principal per login, so a SASL broker lets each login write only its own writer topic:

* a login that publishes an install request (it is in a host row's ``allowed_publishers``) holds WRITE and DESCRIBE, TOPIC, LITERAL, on its own writer command topic and on the shared command topic the publisher also writes (the orchestrator's typed input), and nothing else for that role;
* a host principal (a host row's ``host_principal``) holds, for every login any of its rows allows, READ and DESCRIBE, TOPIC, LITERAL, on that login's writer topic, and WRITE and DESCRIBE, TOPIC, LITERAL, on its own result topic and the shared success and failure terminals, and READ and DESCRIBE, GROUP, PREFIXED, on the group prefix;
* a login that is both is one principal holding the union of its grants, with no duplicate grant.

Principals are sorted by login and grants by (resource type, name, operation), so the output is deterministic.
"""

from __future__ import annotations

from omnimarket.models.broker_acl.model_broker_acl_overlay import (
    EnumAclOperation,
    EnumAclPattern,
    EnumAclResourceType,
    ModelBrokerAclGrant,
    ModelBrokerAclPrincipal,
    ModelBrokerAclScopeOverlay,
)
from omnimarket.nodes.node_install_request_acl_compute.models.model_install_request_acl import (
    ModelInstallAclRequest,
)


def permits(
    overlay: ModelBrokerAclScopeOverlay,
    login: str,
    resource_type: EnumAclResourceType,
    name: str,
    operation: EnumAclOperation,
) -> bool:
    """Whether the principal ``User:<login>`` holds a grant of that resource type and operation.

    A grant matches when its name is LITERAL and equals ``name``, or PREFIXED and is a prefix of ``name``.
    """
    principal = f"User:{login}"
    return any(
        grant.resource_type is resource_type
        and grant.operation is operation
        and (
            grant.name == name
            if grant.pattern is EnumAclPattern.LITERAL
            else name.startswith(grant.name)
        )
        for row in overlay.principals
        if row.principal == principal
        for grant in row.grants
    )


_PUBLISHER_ROLE = "install request publisher: writes only its own writer topic"
_HOST_ROLE = "install request host: reads the writer topics of the logins its row allows and writes its results"
_BOTH_ROLE = "install request publisher and host"

_Grant = tuple[EnumAclResourceType, EnumAclPattern, str, EnumAclOperation]


class HandlerInstallRequestAcl:
    """Derive the broker permission declaration from an install request effect overlay."""

    def handle(self, request: ModelInstallAclRequest) -> ModelBrokerAclScopeOverlay:
        """Build the overlay: one principal per login, each writing only its own writer topic."""
        topics = request.topics
        grants: dict[str, set[_Grant]] = {}
        roles: dict[str, set[str]] = {}

        def topic_rows(
            login: str,
            role: str,
            names: tuple[str, ...],
            operations: tuple[EnumAclOperation, ...],
        ) -> None:
            roles.setdefault(login, set()).add(role)
            grants.setdefault(login, set()).update(
                (EnumAclResourceType.TOPIC, EnumAclPattern.LITERAL, name, operation)
                for name in names
                for operation in operations
            )

        write = (EnumAclOperation.WRITE, EnumAclOperation.DESCRIBE)
        read = (EnumAclOperation.READ, EnumAclOperation.DESCRIBE)
        allowed_by_host_login: dict[str, dict[str, None]] = {}
        for row in request.overlay.hosts:
            allowed_by_host_login.setdefault(row.host_principal, {}).update(
                dict.fromkeys(row.allowed_publishers)
            )
            for login in row.allowed_publishers:
                # A publisher writes its own writer topic and the shared command topic the orchestrator reads.
                topic_rows(
                    login,
                    _PUBLISHER_ROLE,
                    (topics.writer_command_topic(login), topics.command),
                    write,
                )
        for host_login, allowed in allowed_by_host_login.items():
            topic_rows(
                host_login,
                _HOST_ROLE,
                tuple(topics.writer_command_topic(login) for login in allowed),
                read,
            )
            topic_rows(
                host_login,
                _HOST_ROLE,
                (topics.result_topic(host_login), topics.success, topics.failure),
                write,
            )
            grants[host_login].update(
                (
                    EnumAclResourceType.GROUP,
                    EnumAclPattern.PREFIXED,
                    request.group_prefix,
                    operation,
                )
                for operation in read
            )

        principals = tuple(
            ModelBrokerAclPrincipal(
                principal=f"User:{login}",
                role=_BOTH_ROLE if len(roles[login]) > 1 else next(iter(roles[login])),
                grants=tuple(
                    ModelBrokerAclGrant(
                        resource_type=resource_type,
                        pattern=pattern,
                        name=name,
                        operation=operation,
                    )
                    for resource_type, pattern, name, operation in sorted(
                        grants[login], key=lambda g: (g[0], g[2], g[3])
                    )
                ),
            )
            for login in sorted(grants)
        )
        cells = request.cells
        return ModelBrokerAclScopeOverlay(**cells.model_dump(), principals=principals)


__all__: list[str] = ["HandlerInstallRequestAcl", "permits"]
