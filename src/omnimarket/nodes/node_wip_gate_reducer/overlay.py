# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ``wip_limits`` overlay of the WIP gate reducer.

Shape only; the values (repository names, limits, merge rates, the enforcing
ruling) belong to the deployment owner. The caller loads the overlay once and
passes the resulting policy to the gate; a handler never reads it.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)
from omnimarket.models.wip_gate import ModelWipLimit, ModelWipPolicy

OVERLAY_ID = "wip_limits"


class ModelWipLimitsOverlay(BaseModel):
    """``<root>/wip_limits/overlay.yaml``; every key is required."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    enforce_ruling: str
    dispatchable_repos: tuple[str, ...]
    limits: dict[str, ModelWipLimit]
    exempt_roles: tuple[str, ...]

    def policy(self) -> ModelWipPolicy:
        """The overlay as the gate's policy, refusing a dispatchable repo with no limit."""
        for repo in self.dispatchable_repos:
            if repo not in self.limits:
                raise NodeOverlayError(
                    f"{OVERLAY_ID}.limits.{repo} is missing: every dispatchable "
                    f"repository needs a limit with its evidence"
                )
        return ModelWipPolicy(
            enforce_ruling=self.enforce_ruling,
            dispatchable_repos=self.dispatchable_repos,
            limits=self.limits,
            exempt_roles=self.exempt_roles,
        )


def load_wip_policy(environ: Mapping[str, str] | None = None) -> ModelWipPolicy:
    """Load and validate the ``wip_limits`` overlay; refuses naming the missing key."""
    return load_structured_overlay(OVERLAY_ID, ModelWipLimitsOverlay, environ).policy()
