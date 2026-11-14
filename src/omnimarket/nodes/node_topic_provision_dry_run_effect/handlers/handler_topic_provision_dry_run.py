# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerTopicProvisionDryRun: plan the provision rule against the live broker, read-only."""

from __future__ import annotations

from pathlib import Path

from omnimarket.models.topic_provision.model_topic_provision_plan import (
    ModelTopicProvisionPlan,
)
from omnimarket.models.topic_provision.model_topic_provision_request import (
    ModelTopicProvisionRequest,
)
from omnimarket.nodes.node_topic_provision_dry_run_effect.adapters.broker_topics import (
    ProtocolBrokerTopicLister,
)
from omnimarket.nodes.node_topic_provision_dry_run_effect.adapters.contract_facts import (
    read_contract_facts,
)
from omnimarket.nodes.node_topic_provision_dry_run_effect.models.model_topic_provision_dry_run_request import (
    ModelTopicProvisionDryRunRequest,
)
from omnimarket.nodes.node_topic_provision_plan_compute.handlers.handler_topic_provision_plan import (
    TopicProvisionRuleError,
    plan_topic_provision,
)


class HandlerTopicProvisionDryRun:
    """Reads the contract tree and lists the live topics; creates and deletes nothing."""

    def __init__(self, lister: ProtocolBrokerTopicLister | None) -> None:
        self._lister = lister

    async def handle(
        self, request: ModelTopicProvisionDryRunRequest
    ) -> ModelTopicProvisionPlan:
        overlay = request.overlay
        if overlay.profile is None:
            raise TopicProvisionRuleError("overlay.profile is required for the dry run")
        if not overlay.contract_roots:
            raise TopicProvisionRuleError("overlay.contract_roots must be non-empty")
        facts = read_contract_facts([Path(root) for root in overlay.contract_roots])
        live = await self._lister.list_topics() if self._lister is not None else ()
        return plan_topic_provision(
            ModelTopicProvisionRequest(
                contracts=facts,
                profile=overlay.profile,
                lane_manifest=overlay.lane_manifest,
                live_topics=live,
                rule=request.rule,
            )
        )
