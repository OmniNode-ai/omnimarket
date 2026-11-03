# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Which machine principals hold a grant role, and the broker grants each role carries (OMN-22139).

Two inputs, two owners. The grant set of each role (``grant_roles`` in ``contract.yaml``) is
this node's. Which machine holds which role is the deployment's: a machine row of the
``developer.principal`` overlay lists the roles it holds in ``grant_roles``. The handler joins the
two and names nothing itself, so binding another host to a role is one overlay row and no code.

It refuses rather than guessing: no overlay, an overlay the core model rejects, or a row naming a
role the contract does not declare all answer ``refused`` with a reason. Refusal details name the
row position and the role, never other overlay values.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from functools import cache
from pathlib import Path
from typing import Any

import yaml
from omnibase_core.enums.enum_developer_principal_role import (
    EnumDeveloperPrincipalRole,
)
from omnibase_core.models.config_overlay import ModelDeveloperPrincipalOverlay

from omnimarket.handlers.node_overlay_reader import (
    OVERLAY_ROOTS_ENV,
    NodeOverlayError,
    find_node_overlay,
    load_structured_overlay,
)
from omnimarket.nodes.node_machine_grant_role_compute.models.model_machine_grant_role import (
    ModelBrokerGrant,
    ModelGrantRoleBinding,
    ModelMachineGrantRoleRequest,
    ModelMachineGrantRoleResult,
    RefusalReason,
)

_CONTRACT = Path(__file__).resolve().parent.parent / "contract.yaml"
_ACL_PRINCIPAL_PREFIX = "User:"


@cache
def _contract() -> Mapping[str, Any]:
    loaded = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    if not isinstance(loaded, Mapping):
        raise ValueError(f"{_CONTRACT} must be a mapping")
    return loaded


def overlay_id() -> str:
    """The overlay the bindings come from, as the contract names it."""
    return str(_contract()["overlay"]["id"])


@cache
def declared_grant_roles() -> Mapping[str, tuple[ModelBrokerGrant, ...]]:
    """Each declared role's grants: write on its write topics, read and describe on its read topics."""
    roles: dict[str, tuple[ModelBrokerGrant, ...]] = {}
    for name, spec in _contract()["grant_roles"].items():
        grants = [
            ModelBrokerGrant(resource_name=topic, operation="write")
            for topic in spec.get("write_topics", ())
        ]
        for topic in spec.get("read_topics", ()):
            grants.append(ModelBrokerGrant(resource_name=topic, operation="read"))
            grants.append(ModelBrokerGrant(resource_name=topic, operation="describe"))
        roles[str(name)] = tuple(grants)
    return roles


def _refused(
    refusal: RefusalReason, detail: str, declared: tuple[str, ...]
) -> ModelMachineGrantRoleResult:
    return ModelMachineGrantRoleResult(
        status="refused",
        refusal=refusal,
        detail=detail,
        declared_roles=declared,
    )


def _environ(request: ModelMachineGrantRoleRequest) -> Mapping[str, str] | None:
    """``None`` reads the process environment; given roots stand in for it."""
    if request.overlay_roots is None:
        return None
    return {OVERLAY_ROOTS_ENV: os.pathsep.join(request.overlay_roots)}


def resolve(request: ModelMachineGrantRoleRequest) -> ModelMachineGrantRoleResult:
    """Join the overlay's machine role bindings with the contract's grant sets."""
    roles = declared_grant_roles()
    declared = tuple(sorted(roles))
    environ = _environ(request)
    if request.overlay is None and find_node_overlay(overlay_id(), environ) is None:
        return _refused(
            "overlay-missing",
            f"no {overlay_id()} overlay passed or found under {OVERLAY_ROOTS_ENV}",
            declared,
        )
    try:
        overlay = load_structured_overlay(
            overlay_id(),
            ModelDeveloperPrincipalOverlay,
            environ,
            explicit=request.overlay,
        )
    except NodeOverlayError as exc:
        return _refused("overlay-invalid", str(exc), declared)
    bindings: list[ModelGrantRoleBinding] = []
    for index, row in enumerate(overlay.principals):
        if row.role is not EnumDeveloperPrincipalRole.MACHINE:
            continue
        for role in row.grant_roles:
            if role not in roles:
                return _refused(
                    "grant-role-undeclared",
                    f"principals.{index} binds grant role {role!r}, which the contract "
                    f"does not declare (declared {list(declared)})",
                    declared,
                )
            if request.role is not None and role != request.role:
                continue
            if request.principal is not None and row.principal != request.principal:
                continue
            bindings.append(
                ModelGrantRoleBinding(
                    principal=row.principal,
                    acl_principal=f"{_ACL_PRINCIPAL_PREFIX}{row.principal}",
                    device=row.device,
                    role=role,
                    grants=roles[role],
                )
            )
    return ModelMachineGrantRoleResult(
        status="resolved", declared_roles=declared, bindings=tuple(bindings)
    )


class HandlerMachineGrantRole:
    """Runtime entry: the typed request in, the typed bindings out."""

    def handle(
        self, request: ModelMachineGrantRoleRequest
    ) -> ModelMachineGrantRoleResult:
        return resolve(request)


__all__ = ["HandlerMachineGrantRole", "declared_grant_roles", "overlay_id", "resolve"]
