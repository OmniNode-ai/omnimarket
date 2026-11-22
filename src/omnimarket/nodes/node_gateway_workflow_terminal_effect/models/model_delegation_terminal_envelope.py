# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The input of the terminal writer: one delegation terminal event as delivered."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ModelDelegationTerminalEnvelope(BaseModel):
    """One delegation-completed or delegation-failed delivery.

    ``topic`` is the canonical topic name the delivery arrived on; ``payload`` is the flat
    ModelDelegationResult wire object. It is kept as a plain mapping because validation
    of every field, with the dead-letter precedence, is the handler's job.

    The runtime hands an in-process handler one flat dict: the wire fields beside the keys it
    injects (``_topic``, ``_event_type``, ...). A dict without a ``payload`` key is read that
    way: the injected keys are lifted out and every other key is the payload.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    topic: str = ""
    event_type: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _lift_injected_delivery(cls, data: Any) -> Any:
        if not isinstance(data, dict) or "payload" in data:
            return data
        return {
            "topic": str(data.get("_topic") or data.get("topic") or ""),
            "event_type": str(data.get("_event_type") or ""),
            "payload": {k: v for k, v in data.items() if not str(k).startswith("_")},
        }


__all__: list[str] = ["ModelDelegationTerminalEnvelope"]
