# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Derive broker permission rows from contract topics and declared principals (OMN-19998, issue L1-04).

Pure: the topic uses are read from the contracts at composition (contract_topics.py) and the principals from their overlay.
The operations a publisher or a subscriber needs, and the consumer group prefix, are the contract's config, never a
constant here. A row passes the same refusals the scoped render applies; a topic with no row (derived or declared)
is listed and refuses the check, so a new topic with no row fails it.
"""

from __future__ import annotations

from collections import defaultdict

from omnimarket.models.broker_acl import (
    EnumAclOperation,
    EnumAclPattern,
    EnumAclResourceType,
    EnumBrokerAclFindingCode,
    EnumTopicDirection,
    ModelBrokerAclDeriveRequest,
    ModelBrokerAclDeriveResult,
    ModelBrokerAclFinding,
    ModelBrokerAclGrant,
    ModelBrokerAclRow,
    ModelContractTopicUse,
)
from omnimarket.nodes.node_broker_acl_overlay_render_compute.handlers.handler_broker_acl_overlay_render import (
    _grant_findings,
    _row_key,
    contract_config,
)

_CODES = EnumBrokerAclFindingCode


class HandlerBrokerAclDerive:
    def handle(
        self, request: ModelBrokerAclDeriveRequest
    ) -> ModelBrokerAclDeriveResult:
        config = contract_config()
        findings: list[ModelBrokerAclFinding] = []
        uses_by_node: dict[str, list[ModelContractTopicUse]] = defaultdict(list)
        for use in request.topic_uses:
            uses_by_node[use.node].append(use)
        exclusive: dict[str, str] = {}
        principals_of: dict[str, set[str]] = defaultdict(set)
        checked: set[str] = set()
        no_contract: set[str] = set()

        def check_principal(principal: str) -> None:
            if principal in checked:
                return
            checked.add(principal)
            if (
                not principal.startswith(config["principal_prefix"])
                or principal == config["principal_wildcard"]
            ):
                findings.append(
                    ModelBrokerAclFinding(
                        code=_CODES.PRINCIPAL_INVALID, detail=principal
                    )
                )

        def note_node(node: str) -> None:
            if node not in uses_by_node and node not in no_contract:
                no_contract.add(node)
                findings.append(
                    ModelBrokerAclFinding(
                        code=_CODES.NODE_WITHOUT_CONTRACT, detail=node
                    )
                )

        for entry in request.principals.principals:
            check_principal(entry.principal)
            for node in entry.nodes:
                if node in exclusive and exclusive[node] != entry.principal:
                    findings.append(
                        ModelBrokerAclFinding(
                            code=_CODES.NODE_PRINCIPAL_CONFLICT,
                            detail=f"{node}: {exclusive[node]} and {entry.principal}",
                        )
                    )
                    continue
                exclusive[node] = entry.principal
                principals_of[node].add(entry.principal)
                note_node(node)
        # A role is a unit run under several logins at once (every lab host): each member holds the rows of each node.
        for role in request.principals.roles:
            for principal in role.principals:
                check_principal(principal)
            for node in role.nodes:
                principals_of[node].update(role.principals)
                note_node(node)
        rows: dict[tuple[str, str, str, str, str], ModelBrokerAclRow] = {}

        def add(
            principal: str,
            resource: EnumAclResourceType,
            pattern: EnumAclPattern,
            name: str,
            operations: list[str],
        ) -> None:
            for operation in operations:
                grant = ModelBrokerAclGrant(
                    resource_type=resource,
                    pattern=pattern,
                    name=name,
                    operation=EnumAclOperation(operation),
                )
                findings.extend(_grant_findings(principal, grant, config))
                row = ModelBrokerAclRow(principal=principal, **grant.model_dump())
                rows[_row_key(row)] = row

        if request.only_node is not None and request.only_node not in principals_of:
            findings.append(
                ModelBrokerAclFinding(
                    code=_CODES.NODE_WITHOUT_PRINCIPAL, detail=request.only_node
                )
            )
        for node, principal in sorted(
            (node, principal)
            for node, members in principals_of.items()
            for principal in members
        ):
            if request.only_node is not None and node != request.only_node:
                continue
            uses = uses_by_node.get(node, [])
            for use in uses:
                operations = (
                    config["derive_publish_operations"]
                    if use.direction == EnumTopicDirection.PUBLISH
                    else config["derive_subscribe_operations"]
                )
                add(
                    principal,
                    EnumAclResourceType.TOPIC,
                    EnumAclPattern.LITERAL,
                    use.topic,
                    operations,
                )
            if any(use.direction == EnumTopicDirection.SUBSCRIBE for use in uses):
                add(
                    principal,
                    EnumAclResourceType.GROUP,
                    EnumAclPattern.PREFIXED,
                    config["derive_group_prefix_template"].format(node=node),
                    config["derive_group_operations"],
                )
        covered = {
            row.name
            for row in rows.values()
            if row.resource_type == EnumAclResourceType.TOPIC
        }
        covered |= {
            row.name
            for row in request.declared_rows
            if row.resource_type == EnumAclResourceType.TOPIC
        }
        topics = {
            use.topic
            for use in request.topic_uses
            if request.only_node is None or use.node == request.only_node
        }
        uncovered = tuple(sorted(topics - covered))
        findings.extend(
            ModelBrokerAclFinding(code=_CODES.TOPIC_WITHOUT_ROW, detail=topic)
            for topic in uncovered
        )
        hard = any(finding.code != _CODES.TOPIC_WITHOUT_ROW for finding in findings)
        derived = () if hard else tuple(sorted(rows.values(), key=_row_key))
        return ModelBrokerAclDeriveResult(
            scope=request.scope,
            status="refused" if findings else "derived",
            rows=derived,
            contract_topics=len(topics),
            derived_rows=len(derived),
            uncovered_topics=uncovered,
            findings=tuple(findings),
        )
