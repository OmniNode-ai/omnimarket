# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Release sibling premise compute: one release-train premise as a pure function.

``HandlerSiblingPremise.handle(ModelSiblingPremiseRequest) -> ModelSiblingPremiseVerdict``
(definition-B). The node answers the release train's one question per repository:
"does the dev lane run candidate revision X?" It reads the R2 receipts and
determines whether the lane vendors the candidate or a descendant of it.
No clock, no I/O, no model, no envelope.
"""

from omnimarket.nodes.node_release_sibling_premise_compute.handlers.handler_sibling_premise import (
    HandlerSiblingPremise,
)


class NodeReleaseSiblingPremiseCompute(HandlerSiblingPremise):
    """ONEX entry-point wrapper for HandlerSiblingPremise."""


__all__ = [
    "HandlerSiblingPremise",
    "NodeReleaseSiblingPremiseCompute",
]
