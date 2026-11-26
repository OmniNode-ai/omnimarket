# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The tenant topic provision broker settings: the broker lane and the shape of the topics it creates.

The partition count and the replication factor are declared in this node's ``contract.yaml`` under ``config.tenant_topics``.
The lane is named by the environment variable the contract's ``broker_lane_env`` names, and has no default: it names the
broker the way ``onex scm`` resolves one, so a lane that is not the dev lane is the operator's choice and never this code's.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from contextlib import AbstractContextManager, nullcontext
from importlib import resources

import yaml
from pydantic import BaseModel, ConfigDict, Field

NODE_NAME = "node_tenant_topic_provision_effect"


class ModelTenantTopicProvisionSettings(BaseModel):
    """The lane and the topic shape."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str = Field(min_length=1)
    num_partitions: int = Field(ge=1)
    replication_factor: int = Field(ge=1)


def load_settings(
    environ: Mapping[str, str] | None = None,
) -> ModelTenantTopicProvisionSettings:
    """The settings: counts from the contract, the lane from the environment variable the contract names."""
    env = os.environ if environ is None else environ
    text = (
        resources.files(f"omnimarket.nodes.{NODE_NAME}")
        .joinpath("contract.yaml")
        .read_text(encoding="utf-8")
    )
    config = yaml.safe_load(text)["config"]["tenant_topics"]
    lane_env = str(config["broker_lane_env"])
    lane = env.get(lane_env, "")
    if not lane:
        raise ValueError(f"{lane_env} names no broker lane; set it to the lane")
    return ModelTenantTopicProvisionSettings(
        lane=lane,
        num_partitions=config["num_partitions"],
        replication_factor=config["replication_factor"],
    )


def build_broker(
    settings: ModelTenantTopicProvisionSettings,
) -> tuple[object, object, AbstractContextManager[object]]:
    """The lister, the creator and the transport binding for the lane the settings name."""
    from omnibase_infra.cli.delegate_lane_credentials import resolve_lane_binding
    from omnibase_infra.event_bus.lane_client_transport_binding import (
        bind_lane_client_transport,
    )

    from omnimarket.nodes.node_topic_provision_dry_run_effect.adapters.broker_topics import (
        AiokafkaBrokerTopicCreator,
        AiokafkaBrokerTopicLister,
    )

    selection, transport = resolve_lane_binding(settings.lane)
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
    "ModelTenantTopicProvisionSettings",
    "build_broker",
    "load_settings",
]
