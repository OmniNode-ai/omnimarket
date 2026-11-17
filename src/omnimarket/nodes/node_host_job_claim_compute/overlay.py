# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_host_job_claim_compute: every setting of the read, none in code.

Found at ``<root>/node_host_job_claim_compute/overlay.yaml`` under a root named in
``ONEX_SKILL_OVERLAY_ROOTS``. The package ships ``overlay.example.yaml`` with synthetic values.
Nothing here has a default: an absent key refuses the load and names it, and a missing overlay
refuses the run, so no role, host or unit directory is ever spelled in code.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.nodes.node_host_job_claim_compute.models import ModelHostRoleBaseline

NODE_NAME = "node_host_job_claim_compute"


class ModelHostRoleAssignment(BaseModel):
    """One host and the role its jobs are read against."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str = Field(min_length=1)
    role: str = Field(min_length=1)


class ModelHostJobClaimOverlay(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    roles: tuple[ModelHostRoleBaseline, ...] = Field(min_length=1)
    hosts: tuple[ModelHostRoleAssignment, ...] = Field(min_length=1)
    unit_dirs: tuple[str, ...] = Field(min_length=1)
    unit_suffix: str = Field(min_length=1)

    @model_validator(mode="after")
    def _shape(self) -> ModelHostJobClaimOverlay:
        roles = [role.role for role in self.roles]
        if len(roles) != len(set(roles)):
            raise ValueError("a role is declared twice")
        hosts = [assignment.host for assignment in self.hosts]
        if len(hosts) != len(set(hosts)):
            raise ValueError("a host is assigned twice")
        declared = set(roles)
        for assignment in self.hosts:
            if assignment.role not in declared:
                raise ValueError(
                    f"host {assignment.host} is assigned undeclared role {assignment.role}"
                )
        return self


def load_overlay(
    environ: Mapping[str, str] | None = None,
) -> ModelHostJobClaimOverlay:
    return load_structured_overlay(NODE_NAME, ModelHostJobClaimOverlay, environ)
