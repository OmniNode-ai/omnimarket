# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Validate the typed observation inside an enriched bus payload, without envelope imports."""

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, model_validator

from omnimarket.events.pr_state import (
    ModelPrStateObservedEvent,
    pr_state_event_from_wire,
)


class ModelPrStateFoldRequest(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    event: ModelPrStateObservedEvent

    @model_validator(mode="before")
    @classmethod
    def from_wire(cls, value: object) -> object:
        if isinstance(value, Mapping) and "event" not in value:
            # Strip transport enrichment only at this boundary. The event itself
            # stays strict; JSON validation handles wire arrays and enum strings.
            # Either schema version folds: the projection keeps the version 1 columns.
            return {"event": pr_state_event_from_wire(value)}
        return value
