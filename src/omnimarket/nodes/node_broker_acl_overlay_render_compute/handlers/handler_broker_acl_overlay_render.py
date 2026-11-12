# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Render a scoped overlay's desired broker rows and refuse what the apply tool would refuse (OMN-23589).

Pure: the overlay arrives on the request, loaded at composition by overlay.py. The refusals are the apply tool's own
(its declaration loader): a principal that is not a concrete User:, a wildcard or empty resource name,
DELETE on anything but a group, a cluster grant that is not the literal cluster, a duplicate row, and a grant that
would let a lab login read the write node's commands or forge its terminals. The refused set is the contract's
config, never a constant here.
"""

from __future__ import annotations

from collections.abc import Mapping
from importlib import resources
from typing import Any

import yaml

from omnimarket.models.broker_acl import (
    EnumAclOperation,
    EnumAclPattern,
    EnumAclResourceType,
    EnumBrokerAclFindingCode,
    ModelBrokerAclFinding,
    ModelBrokerAclGrant,
    ModelBrokerAclRenderRequest,
    ModelBrokerAclRenderResult,
    ModelBrokerAclRow,
)

_NODE = "node_broker_acl_overlay_render_compute"


def contract_config() -> dict[str, Any]:
    text = (
        resources.files(f"omnimarket.nodes.{_NODE}")
        .joinpath("contract.yaml")
        .read_text(encoding="utf-8")
    )
    loaded = yaml.safe_load(text)
    config = loaded.get("config") if isinstance(loaded, dict) else None
    if not isinstance(config, dict):
        raise ValueError(f"{_NODE}: contract config must be a mapping")
    return config


def _row_key(row: ModelBrokerAclRow) -> tuple[str, str, str, str, str]:
    return (
        row.principal,
        row.resource_type.value,
        row.pattern.value,
        row.name,
        row.operation.value,
    )


def _grant_findings(
    principal: str, grant: ModelBrokerAclGrant, config: Mapping[str, Any]
) -> list[ModelBrokerAclFinding]:
    found: list[ModelBrokerAclFinding] = []
    where = (
        f"{principal} {grant.resource_type.value} {grant.name} {grant.operation.value}"
    )
    if not grant.name.strip() or grant.name.strip() == "*":
        found.append(
            ModelBrokerAclFinding(
                code=EnumBrokerAclFindingCode.RESOURCE_NAME_INVALID, detail=where
            )
        )
    if (
        grant.operation == EnumAclOperation.DELETE
        and grant.resource_type.value not in config["delete_allowed_resource_types"]
    ):
        found.append(
            ModelBrokerAclFinding(
                code=EnumBrokerAclFindingCode.DELETE_NOT_ON_GROUP, detail=where
            )
        )
    if grant.resource_type == EnumAclResourceType.CLUSTER and (
        grant.name != config["cluster_resource_name"]
        or grant.pattern != EnumAclPattern.LITERAL
    ):
        found.append(
            ModelBrokerAclFinding(
                code=EnumBrokerAclFindingCode.CLUSTER_NOT_LITERAL, detail=where
            )
        )
    if grant.resource_type == EnumAclResourceType.TOPIC:
        commands = config["scm_command_topics"]
        terminals = config["scm_terminal_topics"]
        prefixed = grant.pattern == EnumAclPattern.PREFIXED and any(
            topic.startswith(grant.name) for topic in (*commands, *terminals)
        )
        if (
            (grant.name in commands and grant.operation == EnumAclOperation.READ)
            or (grant.name in terminals and grant.operation == EnumAclOperation.WRITE)
            or prefixed
        ):
            found.append(
                ModelBrokerAclFinding(
                    code=EnumBrokerAclFindingCode.FORBIDDEN_SCM_GRANT, detail=where
                )
            )
    return found


class HandlerBrokerAclOverlayRender:
    def handle(
        self, request: ModelBrokerAclRenderRequest
    ) -> ModelBrokerAclRenderResult:
        config = contract_config()
        overlay = request.overlay
        findings: list[ModelBrokerAclFinding] = []
        if overlay.scope != request.scope:
            findings.append(
                ModelBrokerAclFinding(
                    code=EnumBrokerAclFindingCode.SCOPE_MISMATCH,
                    detail=f"requested {request.scope}, overlay says {overlay.scope}",
                )
            )
        rows: list[ModelBrokerAclRow] = []
        for principal in overlay.principals:
            if (
                not principal.principal.startswith(config["principal_prefix"])
                or principal.principal == config["principal_wildcard"]
            ):
                findings.append(
                    ModelBrokerAclFinding(
                        code=EnumBrokerAclFindingCode.PRINCIPAL_INVALID,
                        detail=principal.principal,
                    )
                )
            for grant in principal.grants:
                findings.extend(_grant_findings(principal.principal, grant, config))
                rows.append(
                    ModelBrokerAclRow(
                        principal=principal.principal, **grant.model_dump()
                    )
                )
        keys = [_row_key(row) for row in rows]
        for key in sorted({key for key in keys if keys.count(key) > 1}):
            findings.append(
                ModelBrokerAclFinding(
                    code=EnumBrokerAclFindingCode.DUPLICATE_GRANT,
                    detail=" ".join(key),
                )
            )
        if findings:
            return ModelBrokerAclRenderResult(
                scope=request.scope, status="refused", findings=tuple(findings)
            )
        base = overlay.operator_command.format(
            declaration=overlay.declaration_path.format(scope=request.scope),
            ssh_target=overlay.operator_ssh_target,
        )
        return ModelBrokerAclRenderResult(
            scope=request.scope,
            status="rendered",
            rows=tuple(sorted(rows, key=_row_key)),
            declared=len(rows),
            operator_dry_run=f"{base} --dry-run",
            operator_apply=base,
        )
