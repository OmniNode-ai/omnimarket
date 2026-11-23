# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Read the lab model servers' queues once per routing decision (OMN-26534).

The routing reducer's pick is a pure function of its inputs, so the load the
pick reads is an input like the quota snapshot: this is the caller-side read,
taken at decision time for every endpoint the routing declarations place in a
spread group, and passed into ``delta`` / ``backend_id_for_tier`` as
``load_state``. No endpoint is named here; the group comes from the declared
placements.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from omnimarket.handlers.routing.endpoint_load_reader import (
    MetricsFetch,
    read_endpoint_load,
)
from omnimarket.models.delegation.model_endpoint_load_snapshot import (
    ModelEndpointLoadSnapshot,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
    spread_load_targets,
)

logger = logging.getLogger(__name__)


def read_spread_load_snapshot(
    *, fetch: MetricsFetch | None = None
) -> ModelEndpointLoadSnapshot | None:
    """The load of every spread-group endpoint now, or ``None`` with none declared.

    A deployment that declares no spread placement has one lab endpoint per rung
    and nothing to choose between, so nothing is read and routing is unchanged.
    """
    targets = spread_load_targets()
    if not targets:
        return None
    snapshot = read_endpoint_load(targets, as_of=datetime.now(UTC), fetch=fetch)
    logger.info("delegation load read: %s", snapshot.describe())
    return snapshot


__all__: list[str] = ["read_spread_load_snapshot"]
