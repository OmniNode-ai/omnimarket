# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Compose the rules effect and the revision compute into one check (OMN-20689).

Reads the plugin sync rules from their overlay via node_host_drift_plugin_rules_effect, then hands
them to the pure revision compute. A refused rules load leaves the item alert-only upstream.
"""

from __future__ import annotations

from typing import Protocol

from omnimarket.models.model_plugin_revision import (
    ModelPluginProbeFacts,
    ModelPluginRevisionFacts,
    ModelPluginRevisionVerdict,
)
from omnimarket.models.model_plugin_rules import (
    EnumPluginRulesOutcome,
    ModelPluginRulesRequest,
    ModelPluginRulesResult,
)
from omnimarket.nodes.node_host_drift_plugin_revision_compute.handlers.handler_plugin_revision import (
    HandlerHostDriftPluginRevision,
)
from omnimarket.nodes.node_host_drift_plugin_revision_orchestrator.models.model_plugin_revision_check import (
    EnumPluginRevisionCheckOutcome,
    ModelPluginRevisionChecked,
)
from omnimarket.nodes.node_host_drift_plugin_rules_effect.handlers.handler_plugin_rules import (
    HandlerHostDriftPluginRules,
)


class ProtocolPluginRulesSource(Protocol):
    """What the orchestrator needs of the rules effect: read the rules for one request."""

    def handle(self, request: ModelPluginRulesRequest) -> ModelPluginRulesResult: ...


class HandlerHostDriftPluginRevisionCheck:
    """Orchestrator: rules effect then revision compute, one typed result."""

    def __init__(
        self,
        rules_effect: ProtocolPluginRulesSource | None = None,
        compute: HandlerHostDriftPluginRevision | None = None,
    ) -> None:
        self._rules_effect = (
            rules_effect if rules_effect is not None else HandlerHostDriftPluginRules()
        )
        self._compute = (
            compute if compute is not None else HandlerHostDriftPluginRevision()
        )

    def handle(self, request: ModelPluginProbeFacts) -> ModelPluginRevisionChecked:
        correlation_id = f"plugin-revision:{request.name}"
        rules_result = self._rules_effect.handle(
            ModelPluginRulesRequest(correlation_id=correlation_id)
        )
        if rules_result.outcome == EnumPluginRulesOutcome.REFUSED:
            return ModelPluginRevisionChecked(
                correlation_id=correlation_id,
                outcome=EnumPluginRevisionCheckOutcome.RULES_REFUSED,
                detail=rules_result.detail,
            )
        facts = ModelPluginRevisionFacts.model_validate(
            {**request.model_dump(), "rules": rules_result.rules}
        )
        verdict: ModelPluginRevisionVerdict = self._compute.handle(facts)
        return ModelPluginRevisionChecked(
            correlation_id=correlation_id,
            outcome=EnumPluginRevisionCheckOutcome.CHECKED,
            verdict=verdict,
        )
