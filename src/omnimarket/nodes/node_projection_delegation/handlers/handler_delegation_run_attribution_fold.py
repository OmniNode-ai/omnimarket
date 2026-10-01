# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure fold that keeps a run's stored attribution when a later terminal states none.

A delegation run writes one delegation_events row from two terminals (OMN-20303).
The delegate-skill terminal carries the pinned premium counterfactual, the
session and the delegating actor; the canonical delegation-completed terminal
carries none of them. Written second, it named all three empty and erased them,
while the preserve step kept ``cost_savings_usd`` -- so the row claimed a saving
with no baseline to show for it.

The fold returns the stored columns the incoming row lacks. A terminal that
carries its own value still wins: this keeps evidence, it does not freeze it.
Both effect writers' preserve steps apply what it returns and decide nothing.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field

_ATTRIBUTION_COLUMNS: tuple[str, ...] = (
    "premium_counterfactual",
    "session_id",
    "delegated_by",
)


def _is_unstated(value: object) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


class ModelDelegationRunAttribution(BaseModel):
    """The stored attribution columns the incoming row must keep."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kept: dict[str, object] = Field(default_factory=dict)

    def row_columns(self) -> dict[str, object]:
        """Return the delegation_events columns to carry onto the incoming row."""
        return dict(self.kept)


class ModelDelegationRunAttributionFoldRequest(BaseModel):
    """The stored row for the correlation and the incoming row about to be written."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    stored: Mapping[str, object]
    incoming: Mapping[str, object]


class HandlerDelegationRunAttributionFold:
    """Keep a run's counterfactual, session and actor across its terminals."""

    def handle(
        self, request: ModelDelegationRunAttributionFoldRequest
    ) -> ModelDelegationRunAttribution:
        """Return each attribution column the incoming row leaves unstated and the stored row states."""
        return ModelDelegationRunAttribution(
            kept={
                column: request.stored[column]
                for column in _ATTRIBUTION_COLUMNS
                if _is_unstated(request.incoming.get(column))
                and not _is_unstated(request.stored.get(column))
            }
        )


__all__ = [
    "HandlerDelegationRunAttributionFold",
    "ModelDelegationRunAttribution",
    "ModelDelegationRunAttributionFoldRequest",
]
