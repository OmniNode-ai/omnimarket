# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Injectable read seam for a manifest's labelled items (OMN-19793)."""

from typing import Protocol

from omnimarket.events.delegation_gate_eval.model_gate_eval_item import (
    ModelGateEvalItem,
)


class ProtocolDelegationEvalLabelledItems(Protocol):
    """Read one rater's labels of one manifest, bound to the request tenant.

    Implementations read ``delegation_eval_items`` under the invoking tenant and
    return each labelled item with its recorded prompt, recorded answer and
    recorded gate verdict. An item is in the manifest when its label event
    carried the manifest id in ``computed_facts.manifest_id``.
    """

    def get_labelled_items(
        self, manifest_id: str, rater_role: str, rubric_version: str
    ) -> tuple[ModelGateEvalItem, ...]:
        """Return the labelled items; an empty tuple when none are labelled."""
        ...
