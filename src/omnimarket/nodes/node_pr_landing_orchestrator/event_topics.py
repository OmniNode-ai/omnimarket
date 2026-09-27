# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Frozen class-name to topic routing for the landing workflow's own events.

The delegation canary routes typed terminals by class name through its
contract's ``published_events`` block. This map is that block's wave-1 form:
wave 2 writes one ``published_events`` row per entry, with ``event_type`` the
class name minus its ``Model`` prefix, and a conformance test holds the two
equal from then on. Until the handler exists the contract publishes nothing.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel

from omnimarket.events.topics import (
    PR_LANDING_AGENT_NEEDED_TOPIC_V1,
    PR_LANDING_CLOSED_TOPIC_V1,
    PR_LANDING_MERGED_TOPIC_V1,
    PR_LANDING_TRANSITIONED_TOPIC_V1,
)
from omnimarket.nodes.node_pr_landing_orchestrator.models.model_pr_landing_agent_needed import (
    ModelPrLandingAgentNeeded,
)
from omnimarket.nodes.node_pr_landing_orchestrator.models.model_pr_landing_closed import (
    ModelPrLandingClosed,
)
from omnimarket.nodes.node_pr_landing_orchestrator.models.model_pr_landing_merged import (
    ModelPrLandingMerged,
)
from omnimarket.nodes.node_pr_landing_orchestrator.models.model_pr_landing_transitioned import (
    ModelPrLandingTransitioned,
)

PR_LANDING_EVENT_TOPICS: Mapping[type[BaseModel], str] = {
    ModelPrLandingTransitioned: PR_LANDING_TRANSITIONED_TOPIC_V1,
    ModelPrLandingAgentNeeded: PR_LANDING_AGENT_NEEDED_TOPIC_V1,
    ModelPrLandingMerged: PR_LANDING_MERGED_TOPIC_V1,
    ModelPrLandingClosed: PR_LANDING_CLOSED_TOPIC_V1,
}


__all__: list[str] = ["PR_LANDING_EVENT_TOPICS"]
