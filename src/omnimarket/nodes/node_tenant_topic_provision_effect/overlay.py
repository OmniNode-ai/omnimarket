# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The tenant topic provision overlay: the broker lane and the shape of the topics it creates.

Found at ``<root>/node_tenant_topic_provision_effect/overlay.yaml`` under a root named in ``ONEX_SKILL_OVERLAY_ROOTS``;
the package ships ``overlay.example.yaml`` with synthetic values. Nothing here has a default. The lane names the broker
the way ``onex scm`` resolves one, so a lane that is not the dev lane is the operator's choice and never this code's.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import AbstractContextManager, nullcontext

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.handlers.node_overlay_reader import load_structured_overlay

NODE_NAME = "node_tenant_topic_provision_effect"


class ModelTenantTopicProvisionOverlay(BaseModel):
    """The whole overlay document."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str = Field(min_length=1)
    num_partitions: int = Field(ge=1)
    replication_factor: int = Field(ge=1)


def load_overlay(
    environ: Mapping[str, str] | None = None,
) -> ModelTenantTopicProvisionOverlay:
    """The overlay document, validated."""
    return load_structured_overlay(NODE_NAME, ModelTenantTopicProvisionOverlay, environ)


def build_broker(
    overlay: ModelTenantTopicProvisionOverlay,
) -> tuple[object, object, AbstractContextManager[object]]:
    """The lister, the creator and the transport binding for the lane the overlay names."""
    from omnibase_infra.cli.delegate_lane_credentials import resolve_lane_binding
    from omnibase_infra.event_bus.lane_client_transport_binding import (
        bind_lane_client_transport,
    )

    from omnimarket.nodes.node_topic_provision_dry_run_effect.adapters.broker_topics import (
        AiokafkaBrokerTopicCreator,
        AiokafkaBrokerTopicLister,
    )

    selection, transport = resolve_lane_binding(overlay.lane)
    binding: AbstractContextManager[object] = (
        bind_lane_client_transport(transport)
        if transport is not None
        else nullcontext()
    )
    return (
        AiokafkaBrokerTopicLister(selection.bootstrap_servers),
        AiokafkaBrokerTopicCreator(selection.bootstrap_servers),
        binding,
    )


__all__ = [
    "NODE_NAME",
    "ModelTenantTopicProvisionOverlay",
    "build_broker",
    "load_overlay",
]
