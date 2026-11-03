# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request and result of node_machine_grant_role_compute (OMN-22139)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

RefusalReason = Literal[
    "overlay-missing",
    "overlay-invalid",
    "grant-role-undeclared",
]


class ModelMachineGrantRoleRequest(BaseModel):
    """Which bindings to resolve, and where the developer.principal overlay comes from.

    ``overlay`` is the overlay document itself, injected by the caller. Without it the document
    is read from ``<root>/developer.principal/overlay.yaml`` under ``overlay_roots``, or under
    ``ONEX_SKILL_OVERLAY_ROOTS`` when ``overlay_roots`` is ``None``. ``role`` and ``principal``
    narrow the answer; neither changes what is refused.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    overlay: dict[str, JsonValue] | None = None
    overlay_roots: tuple[str, ...] | None = None
    role: str | None = None
    principal: str | None = None


class ModelBrokerGrant(BaseModel):
    """One broker ACL a binding carries: allow ``operation`` on the literal topic."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    resource_type: Literal["topic"] = "topic"
    resource_name: str
    pattern_type: Literal["literal"] = "literal"
    operation: Literal["write", "read", "describe"]


class ModelGrantRoleBinding(BaseModel):
    """One machine principal holding one grant role, with the grants that role carries."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    principal: str
    acl_principal: str
    device: str
    role: str
    grants: tuple[ModelBrokerGrant, ...]


class ModelMachineGrantRoleResult(BaseModel):
    """The resolved bindings, or the refusal that stopped them."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["resolved", "refused"]
    refusal: RefusalReason | None = None
    detail: str = ""
    declared_roles: tuple[str, ...] = ()
    bindings: tuple[ModelGrantRoleBinding, ...] = Field(default=())


__all__ = [
    "ModelBrokerGrant",
    "ModelGrantRoleBinding",
    "ModelMachineGrantRoleRequest",
    "ModelMachineGrantRoleResult",
    "RefusalReason",
]
