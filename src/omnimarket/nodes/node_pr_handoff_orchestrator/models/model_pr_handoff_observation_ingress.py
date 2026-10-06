# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The PR watcher's observation as this orchestrator consumes it (OMN-20636)."""

from __future__ import annotations

from pydantic import ConfigDict, Field, model_validator

from omnimarket.events.pr_state import ModelPrStateEmitRequest
from omnimarket.models.pr_handoff.model_pr_handoff_requested import handoff_key_for


class ModelPrHandoffObservationIngress(ModelPrStateEmitRequest):
    """One ``pr.state.observed`` payload plus the ``state_io`` key it belongs to.

    The wire payload is the PR watcher's (node_pr_state_emit_effect's) event,
    whose identity is its ``digest``. ``handoff_key`` is derived from ``repo``
    and ``pr_number`` so the runtime loads the PR's handoff row; a payload that
    supplies a different key is refused.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    digest: str = Field(default="", pattern=r"^([0-9a-f]{64})?$")
    handoff_key: str = Field(default="", description="Derived: repo#pr_number.")

    @model_validator(mode="before")
    @classmethod
    def _derive_handoff_key(cls, data: object) -> object:
        if isinstance(data, dict) and "repo" in data and "pr_number" in data:
            derived = handoff_key_for(str(data["repo"]), int(data["pr_number"]))
            supplied = data.get("handoff_key")
            if supplied and supplied != derived:
                msg = f"handoff_key {supplied!r} does not match {derived!r}"
                raise ValueError(msg)
            return {**data, "handoff_key": derived}
        return data

    def observation(self) -> ModelPrStateEmitRequest:
        """The watcher's facts without the routing key and digest."""
        return ModelPrStateEmitRequest.model_validate(
            self.model_dump(mode="json", exclude={"digest", "handoff_key"})
        )


__all__: list[str] = ["ModelPrHandoffObservationIngress"]
