# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The models of node_host_role_baseline_compute (OMN-24263).

The baselines are the node's overlay: what every host carries (`base`) and what each role adds on top (`roles`).
The request names one host, its declared role and where its registry root is; the result is the host's expected
set as the ModelHostProfile the host drift check takes, or a typed refusal.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.model_host_profile import ModelHostProfile, ModelProfileClone
from omnimarket.models.model_host_role import EnumHostRole


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelRoleBaseline(_Frozen):
    """What a role carries: systemd units and clones. Nothing here names a host."""

    systemd_units: tuple[str, ...]
    clones: tuple[ModelProfileClone, ...]
    #: The interpreter of the role's per-clone venvs; null takes the profile defaults' interpreter.
    python: str | None


class ModelProfileDefaults(_Frozen):
    """The profile fields every role shares."""

    python: str = Field(min_length=1)
    git_remote_template: str = Field(min_length=1)
    unit_prefix: str = Field(min_length=1)


class ModelHostRoleBaselines(_Frozen):
    """The overlay: the defaults, the baseline every host carries and one baseline per role."""

    defaults: ModelProfileDefaults
    base: ModelRoleBaseline
    roles: dict[EnumHostRole, ModelRoleBaseline]


class EnumBaselineRefusal(StrEnum):
    NO_ROLE = "no-role"
    NO_BASELINE = "no-baseline"
    CONFLICTING_CLONE = "conflicting-clone"


class ModelHostRoleBaselineRequest(_Frozen):
    host: str = Field(min_length=1)
    #: The role the host table row declares; null when the row declares none.
    host_role: EnumHostRole | None
    registry_root: str = Field(min_length=1)
    #: Null measures and acts on the local host.
    ssh_target: str | None
    baselines: ModelHostRoleBaselines


class ModelHostRoleBaselineResult(_Frozen):
    host: str
    host_role: EnumHostRole | None
    #: The host's expected set; null when the request was refused.
    profile: ModelHostProfile | None
    refusal: EnumBaselineRefusal | None
    detail: str


__all__ = [
    "EnumBaselineRefusal",
    "ModelHostRoleBaselineRequest",
    "ModelHostRoleBaselineResult",
    "ModelHostRoleBaselines",
    "ModelProfileDefaults",
    "ModelRoleBaseline",
]
