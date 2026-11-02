# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Overlay of the node announcement step (OMN-21597).

Every dependency and every configuration value of the step is named here and
nowhere else: the bus, the inventory store and the distribution source are
binding names resolved by ``AnnounceBindings``, so one test runs against the
in-memory bus and against a recording bus by changing only this overlay.
"""

from uuid import UUID

from omnibase_core.models.nodes.package_announce.model_node_announce_policy import (
    ModelNodeAnnouncePolicy,
)
from pydantic import BaseModel, ConfigDict, Field


class ModelNodeAnnounceOverlay(BaseModel):
    """Bindings and policy of one announcer and its local catalog.

    Attributes:
        announcer_id: Identity stamped on every announcement this process publishes.
        announcement_topic: The topic this workload announces on and its catalog reads; a
            per-workload exclusive-writer topic of the contract's announcement family.
        bus_binding: Name of the registered bus factory.
        inventory_store_binding: Name of the registered inventory store factory.
        distribution_source_binding: Name of the registered distribution source factory.
        policy: Trusted handler prefixes, owned profiles and the announcer allowed
            to withdraw each distribution (core's admission policy).
        acknowledgement_attempts: How many times the announcer asks for the catalog outcome.
        acknowledgement_poll_seconds: Wait between those asks (zero on the in-memory bus).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    announcer_id: UUID
    announcement_topic: str = Field(min_length=1)
    bus_binding: str = Field(min_length=1)
    inventory_store_binding: str = Field(min_length=1)
    distribution_source_binding: str = Field(min_length=1)
    policy: ModelNodeAnnouncePolicy
    acknowledgement_attempts: int = Field(ge=1)
    acknowledgement_poll_seconds: float = Field(ge=0)
