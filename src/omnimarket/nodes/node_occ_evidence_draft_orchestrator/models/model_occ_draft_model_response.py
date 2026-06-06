"""Typed parse of the local-model inference content into OCC draft artifacts.

OMN-12580 (Phase 5). The OCC draft orchestrator delegates artifact generation to
a local model over ``onex.cmd.omnibase-infra.delegation-request.v1`` and receives
the generated content on ``onex.evt.omnibase-infra.inference-response.v1``. The
model returns a single JSON document; this model is the deterministic typed parse
of that JSON. The orchestrator never trusts the model to mark its own draft
authoritative — it only extracts the artifact strings, then builds a PROVISIONAL
``ModelOccEvidenceDraft``.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelOccDraftModelResponse(BaseModel):
    """Deterministic parse of the model-generated OCC draft content."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    contract_yaml: str = Field(
        ..., min_length=1, description="Model-generated OCC contract YAML."
    )
    pr_body: str = Field(..., min_length=1, description="Model-generated OCC PR body.")
    receipt_yamls: tuple[str, ...] = Field(
        default_factory=tuple,
        description="Model-generated DoD receipt YAML documents.",
    )


__all__: list[str] = ["ModelOccDraftModelResponse"]
