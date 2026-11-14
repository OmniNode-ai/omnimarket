# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Derive a host's expected set from its declared role: the baseline every host carries plus the role's own (OMN-24263).

Pure: no I/O and no clock. Two hosts of one role get the same units and clones (the request carries no host-specific
entry into them), and a host of another role differs only by the entries of the two roles. A host with no declared
role, a role with no baseline and a role whose clone disagrees with the base clone at the same path are refused with
a typed reason, never guessed.
"""

from __future__ import annotations

from omnimarket.models.model_host_profile import ModelHostProfile, ModelProfileClone
from omnimarket.nodes.node_host_role_baseline_compute.models import (
    EnumBaselineRefusal,
    ModelHostRoleBaselineRequest,
    ModelHostRoleBaselineResult,
)


def _refused(
    request: ModelHostRoleBaselineRequest, why: EnumBaselineRefusal, detail: str
) -> ModelHostRoleBaselineResult:
    return ModelHostRoleBaselineResult(
        host=request.host,
        host_role=request.host_role,
        profile=None,
        refusal=why,
        detail=detail,
    )


class HandlerHostRoleBaseline:
    """``handle(request) -> result``."""

    def handle(
        self, request: ModelHostRoleBaselineRequest
    ) -> ModelHostRoleBaselineResult:
        role = request.host_role
        if role is None:
            return _refused(
                request,
                EnumBaselineRefusal.NO_ROLE,
                f"host {request.host} declares no host_role in the host table",
            )
        baselines = request.baselines
        own = baselines.roles.get(role)
        if own is None:
            return _refused(
                request,
                EnumBaselineRefusal.NO_BASELINE,
                f"role {role.value} has no baseline; declared: "
                f"{sorted(r.value for r in baselines.roles)}",
            )
        base = baselines.base
        clones: dict[str, ModelProfileClone] = {}
        for clone in (*base.clones, *own.clones):
            seen = clones.get(clone.path)
            if seen is not None and seen != clone:
                return _refused(
                    request,
                    EnumBaselineRefusal.CONFLICTING_CLONE,
                    f"role {role.value} declares {clone.repo}@{clone.ref} at {clone.path}, "
                    f"which the base declares as {seen.repo}@{seen.ref}",
                )
            clones[clone.path] = clone
        profile = ModelHostProfile(
            host=request.host,
            ssh_target=request.ssh_target,
            registry_root=request.registry_root,
            python=own.python or baselines.defaults.python,
            git_remote_template=baselines.defaults.git_remote_template,
            unit_prefix=baselines.defaults.unit_prefix,
            systemd_units=tuple(
                dict.fromkeys((*base.systemd_units, *own.systemd_units))
            ),
            clones=tuple(clones.values()),
        )
        return ModelHostRoleBaselineResult(
            host=request.host,
            host_role=role,
            profile=profile,
            refusal=None,
            detail="",
        )


__all__ = ["HandlerHostRoleBaseline"]
