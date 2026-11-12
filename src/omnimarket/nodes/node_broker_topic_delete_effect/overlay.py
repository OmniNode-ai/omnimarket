# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_broker_topic_delete_effect: every setting of the delete, none in code.

Found at ``<root>/node_broker_topic_delete_effect/overlay.yaml`` under a root named in
``ONEX_SKILL_OVERLAY_ROOTS``. The package ships ``overlay.example.yaml`` with synthetic values.
Nothing here has a default: an absent key refuses the load and names it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.nodes.node_broker_topic_removal_effect.overlay import (
    ModelContractSource,
)

NODE_NAME = "node_broker_topic_delete_effect"


class ModelSuperuserAccess(BaseModel):
    """Where the dev broker's superuser is read: only on the broker host, never in this process.

    The cells are the ones the broker permission tool's declaration carries
    (``node_broker_acl_reconcile_effect``): the credential is read by a script that runs on the
    broker host from the named source container, so it never reaches this process, a command line
    or a log. ``lane`` admits the dev lane only: any other value refuses the overlay load.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: Literal["dev"]
    host: str = Field(min_length=1)
    compose_project: str = Field(min_length=1)
    broker_container: str = Field(min_length=1)
    superuser_source_container: str = Field(min_length=1)
    #: Name of the environment variable that holds the ssh target of the broker host.
    ssh_target_env: str = Field(min_length=1)
    #: ``-X brokers=`` value the rpk client uses inside the broker container.
    rpk_brokers: str = Field(min_length=1)
    sasl_mechanism: str = Field(min_length=1)
    script_timeout_seconds: float = Field(gt=0)


class ModelTopicDeleteOverlay(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    contracts: ModelContractSource
    #: Topic name prefixes the delete never touches.
    protected_prefixes: tuple[str, ...]
    #: A topic whose newest retained record is younger than this is still produced to.
    recent_producer_days: int = Field(ge=1)
    #: The most topics one command may name.
    max_topics_per_command: int = Field(ge=1)
    #: Whether a command may issue deletes at all; a report-only run only names what would go.
    enforce: bool
    #: The declared bus lane whose machine credential the broker admin client uses.
    bus_lane: str = Field(min_length=1)
    #: Client id the admin client presents while reading live evidence.
    probe_client_id: str = Field(min_length=1)
    request_timeout_seconds: float = Field(gt=0)
    #: When present the evidence and the deletes run as the dev broker superuser, read on the broker
    #: host; absent, they run as ``bus_lane``'s own machine login.
    superuser: ModelSuperuserAccess | None = None

    @model_validator(mode="after")
    def _superuser_lane_is_the_bus_lane(self) -> ModelTopicDeleteOverlay:
        if self.superuser is not None and self.superuser.lane != self.bus_lane:
            raise ValueError(
                f"superuser.lane {self.superuser.lane!r} differs from bus_lane {self.bus_lane!r}"
            )
        return self


def load_overlay(environ: Mapping[str, str] | None = None) -> ModelTopicDeleteOverlay:
    return load_structured_overlay(NODE_NAME, ModelTopicDeleteOverlay, environ)


__all__ = [
    "NODE_NAME",
    "ModelSuperuserAccess",
    "ModelTopicDeleteOverlay",
    "load_overlay",
]
