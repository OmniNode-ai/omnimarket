# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One event a scenario run saw on the bus."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, JsonValue


class ModelChainScenarioObservedEvent(BaseModel):
    """A produced event: topic, payload and the correlation id it carried and from where."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    topic: str
    payload: dict[str, JsonValue]
    correlation_id: str
    correlation_source: Literal["payload", "runtime"]
    failure: str | None = None


__all__ = ["ModelChainScenarioObservedEvent"]
