# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_delegation_completion_rate_effect: where the rows are read and where the row is appended, none in code.

Found at ``<root>/node_delegation_completion_rate_effect/overlay.yaml`` under a root named in ``ONEX_SKILL_OVERLAY_ROOTS``. The
bounds, the window and the row words are the compute node's overlay (``node_delegation_completion_rate_compute``), loaded beside
this one. Nothing here has a default: an absent key refuses the load and names it.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.model_delegation_completion_rate import (
    ModelDelegationRateOverlay,
)
from omnimarket.nodes.node_reading_writer_effect.overlay import ModelLedgerAppendRoute

NODE_NAME = "node_delegation_completion_rate_effect"
COMPUTE_NODE_NAME = "node_delegation_completion_rate_compute"
_ENV_NAME = r"^[A-Za-z_][A-Za-z0-9_]*$"
_IDENT = r"^[A-Za-z_][A-Za-z0-9_-]*$"


class ModelDelegationEventsSource(BaseModel):
    """Where delegation_events is read: in its postgres container on the dev lane host, over ssh, in a read only transaction."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ssh_host_env: str = Field(pattern=_ENV_NAME)
    compose_project: str = Field(pattern=_IDENT)
    service: str = Field(pattern=_IDENT)
    database: str = Field(pattern=_IDENT)
    db_user: str = Field(pattern=_IDENT)
    read_timeout_seconds: int = Field(ge=1)


class ModelDelegationRatePublish(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    actor: str = Field(min_length=1)


class ModelDelegationRateEffectOverlay(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    events_source: ModelDelegationEventsSource
    ledger_append: ModelLedgerAppendRoute
    publish: ModelDelegationRatePublish


def load_overlay(
    environ: Mapping[str, str] | None = None,
) -> ModelDelegationRateEffectOverlay:
    return load_structured_overlay(NODE_NAME, ModelDelegationRateEffectOverlay, environ)


def load_rate_overlay(
    environ: Mapping[str, str] | None = None,
) -> ModelDelegationRateOverlay:
    """The compute node's overlay: the window, the row words and the floor owner's bounds."""
    return load_structured_overlay(
        COMPUTE_NODE_NAME, ModelDelegationRateOverlay, environ
    )


__all__ = [
    "COMPUTE_NODE_NAME",
    "NODE_NAME",
    "ModelDelegationEventsSource",
    "ModelDelegationRateEffectOverlay",
    "ModelDelegationRatePublish",
    "load_overlay",
    "load_rate_overlay",
]
