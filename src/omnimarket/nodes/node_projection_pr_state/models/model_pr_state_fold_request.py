# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Validate the typed observation inside an enriched bus payload, without envelope imports."""

import json
from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, model_validator

from omnimarket.events.pr_state import (
    ModelPrStateObservedEvent,
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
            payload = {
                k: value[k]
                for k in ModelPrStateObservedEvent.model_fields
                if k in value
            }
            if "digest" not in payload:
                raise ValueError("wire observation must carry digest")
            return {
                "event": ModelPrStateObservedEvent.model_validate_json(
                    json.dumps(payload)
                )
            }
        return value
