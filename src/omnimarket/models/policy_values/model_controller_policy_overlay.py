# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The controller policy overlay: budgets, caps, windows and bounds (OMN-22019).

A budget, cap, window or bound a controller gate judges with is a policy value,
not a module constant: it lives in this overlay, typed, and every value carries
the ruling (a ``RULING`` row's timestamp) or ticket that set it. A value with no
ruling is refused at load, so no number enters a verdict without its authority.

The overlay is read on every call (``load_controller_policy_overlay``), never
cached at import, so raising a value in a deployment's overlay file changes the
gate's verdict on its next call with no release. The shipped default sits beside
this module; ``OMNIMARKET_CONTROLLER_POLICY_OVERLAY`` names a deployment's copy.
"""

from __future__ import annotations

import os
from importlib import resources
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

#: Names a deployment's overlay file in place of the shipped default.
ENV_CONTROLLER_POLICY_OVERLAY = "OMNIMARKET_CONTROLLER_POLICY_OVERLAY"
OVERLAY_SCHEMA_VERSION = "controller-policy-overlay/v1"

_PACKAGE = "omnimarket.models.policy_values"
_DEFAULT_OVERLAY_FILE = "controller_policy_overlay.default.yaml"

#: A ``RULING`` row's timestamp (``2026-10-21T21:14:58Z``) or the ticket that set the value.
RulingId = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        pattern=r"^(?:\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z|OMN-\d+)$",
    ),
]


class ModelRuledValue(BaseModel):
    """One policy value and the ruling that set it; both are required."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    value: int = Field(ge=0)
    ruling: RulingId


class ModelDeadmanPolicy(BaseModel):
    """The automation-liveness dead-man's bounds and budgets."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    primary_heartbeat_bound_seconds: ModelRuledValue
    delivery_budget_seconds: ModelRuledValue
    canary_injection_hour_utc: ModelRuledValue
    canary_receipt_budget_seconds: ModelRuledValue


class ModelCauseClaimOwnershipPolicy(BaseModel):
    """The cause-claim ownership rule's read cap and acting window."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    decisions_tail_bytes: ModelRuledValue
    acting_window_seconds: ModelRuledValue


class ModelControllerPolicyOverlay(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["controller-policy-overlay/v1"]
    automation_liveness_deadman: ModelDeadmanPolicy
    cause_claim_ownership: ModelCauseClaimOwnershipPolicy


def load_controller_policy_overlay(
    path: Path | None = None,
) -> ModelControllerPolicyOverlay:
    """Read and validate the overlay now: ``path``, else the env-named file, else the shipped default."""
    if path is None:
        named = os.environ.get(ENV_CONTROLLER_POLICY_OVERLAY, "").strip()
        path = Path(named) if named else None
    if path is not None:
        text = path.read_text(encoding="utf-8")
    else:
        text = (
            resources.files(_PACKAGE)
            .joinpath(_DEFAULT_OVERLAY_FILE)
            .read_text(encoding="utf-8")
        )
    return ModelControllerPolicyOverlay.model_validate(yaml.safe_load(text))


__all__ = [
    "ENV_CONTROLLER_POLICY_OVERLAY",
    "OVERLAY_SCHEMA_VERSION",
    "ModelCauseClaimOwnershipPolicy",
    "ModelControllerPolicyOverlay",
    "ModelDeadmanPolicy",
    "ModelRuledValue",
    "RulingId",
    "load_controller_policy_overlay",
]
