# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Fill-side bus port. No local evaluation path bypasses the dispatched check node."""

from omnimarket.handlers.container.container_overlay_dependencies import (
    require_dependencies,
)
from omnimarket.models.model_rule_premise import (
    ModelBriefCheckRequest,
    ModelBriefCheckResult,
)
from omnimarket.protocols.protocol_rule_premise import ProtocolRulePremiseGate

GATE_PROTOCOL = "omnimarket.protocols.protocol_rule_premise.ProtocolRulePremiseGate"


class BoundPremiseGate:
    def handle(self, request: ModelBriefCheckRequest) -> ModelBriefCheckResult:
        try:
            gate = require_dependencies([GATE_PROTOCOL])[GATE_PROTOCOL]
            if not isinstance(gate, ProtocolRulePremiseGate):
                raise TypeError("R-B bus gate binding does not satisfy its protocol")
            return gate.handle(request)
        except (
            OSError,
            ValueError,
            TypeError,
            LookupError,
            RuntimeError,
            ImportError,
            AttributeError,
        ) as exc:
            return ModelBriefCheckResult(
                verdict="UNREAD", reason=f"R-B bus gate: {exc}"
            )
