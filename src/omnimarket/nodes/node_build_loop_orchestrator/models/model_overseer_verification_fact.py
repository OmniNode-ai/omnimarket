"""ModelOverseerVerificationFact -- the overseer verifier's completed fact, as the orchestrator sees it.

The orchestrator awaits this fact in-process during VERIFYING (it subscribes to
the topic for the run's correlation id). The same topic is also a declared
subscribe topic of the contract, so the runtime dispatches every fact to a
handler route as well; this model is that route's typed input.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelOverseerVerificationFact(BaseModel):
    """Verifier completion fact. Only the fields the orchestrator reads are named."""

    model_config = ConfigDict(frozen=True, extra="allow")

    correlation_id: str = Field(default="", description="Run the fact answers.")
    passed: bool = Field(default=False, description="Whether verification passed.")


__all__: list[str] = ["ModelOverseerVerificationFact"]
