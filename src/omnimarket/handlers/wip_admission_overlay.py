# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ``wip_admission`` overlay: the limits, each with the measurement behind it.

Shape only; the values (repository names, merge rates, the target lead time, the limits and
the enforcing ruling) belong to the deployment owner. A limit that is not the one its own
measurement gives (``derive_limit``) is refused naming the key, so no limit is a bare number.
"""

from __future__ import annotations

from collections.abc import Mapping
from importlib.resources import files

import yaml
from pydantic import BaseModel, ConfigDict

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)
from omnimarket.handlers.rules_wip_admission import limit_is_derived
from omnimarket.models.wip_admission import (
    EnumWipFixClass,
    ModelWipAdmissionConfig,
    ModelWipLimit,
    ModelWipPolicy,
)

OVERLAY_ID = "wip_admission"
_NODE_PACKAGE = "omnimarket.nodes.node_wip_admission_compute"


def load_wip_admission_config() -> ModelWipAdmissionConfig:
    """The packaged contract's ``config.wip_admission`` block."""
    raw = yaml.safe_load(
        files(_NODE_PACKAGE).joinpath("contract.yaml").read_text("utf-8")
    )
    return ModelWipAdmissionConfig.model_validate(raw["config"]["wip_admission"])


class ModelWipAdmissionOverlay(BaseModel):
    """``<root>/wip_admission/overlay.yaml``; every key is required."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    enforce_ruling: str
    dispatchable_repos: tuple[str, ...]
    fleet: ModelWipLimit
    limits: dict[str, ModelWipLimit]
    fix_forward_classes: tuple[EnumWipFixClass, ...]
    fix_forward_kinds: tuple[str, ...]
    fix_forward_lane_prefixes: tuple[str, ...]
    watcher_state_path: str

    def policy(self, config: ModelWipAdmissionConfig) -> ModelWipPolicy:
        """The overlay as the node's policy, refusing a missing or underived limit by key."""
        for repo in self.dispatchable_repos:
            if repo not in self.limits:
                raise NodeOverlayError(
                    f"{OVERLAY_ID}.limits.{repo} is missing: every dispatchable "
                    f"repository needs a limit with its measurement"
                )
        scopes = {
            "fleet": self.fleet,
            **{f"limits.{r}": v for r, v in self.limits.items()},
        }
        for key, entry in scopes.items():
            if not limit_is_derived(entry, config):
                raise NodeOverlayError(
                    f"{OVERLAY_ID}.{key}.limit is {entry.limit}, which is not the limit "
                    f"its merges_per_hour and lead_time_hours give"
                )
        return ModelWipPolicy(
            enforce_ruling=self.enforce_ruling,
            fleet=self.fleet,
            limits=self.limits,
            fix_forward_classes=self.fix_forward_classes,
            fix_forward_kinds=self.fix_forward_kinds,
            fix_forward_lane_prefixes=self.fix_forward_lane_prefixes,
            watcher_state_path=self.watcher_state_path,
        )


def load_wip_admission_policy(
    environ: Mapping[str, str] | None = None,
) -> ModelWipPolicy:
    """Load and validate the overlay; refuses naming the missing or underived key."""
    overlay = load_structured_overlay(OVERLAY_ID, ModelWipAdmissionOverlay, environ)
    return overlay.policy(load_wip_admission_config())
