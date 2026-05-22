"""Request model for deterministic closeout verification."""

from __future__ import annotations

from omnibase_core.models.pipeline.model_evidence_artifact import ModelEvidenceArtifact
from omnibase_core.models.pipeline.model_golden_chain_entry import ModelGoldenChainEntry
from pydantic import BaseModel, ConfigDict, Field


class ModelCloseoutVerifyRequest(BaseModel):
    """All closeout verification inputs, already materialized by upstream effects."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    expected_chain: tuple[ModelGoldenChainEntry, ...] = Field(default_factory=tuple)
    observed_chain: tuple[ModelGoldenChainEntry, ...] | None = None
    evidence_artifacts: tuple[ModelEvidenceArtifact, ...] = Field(default_factory=tuple)
    required_evidence_kinds: tuple[str, ...] = Field(default_factory=tuple)
    test_result: bool = True
    verifier_identity: str | None = None


__all__ = ["ModelCloseoutVerifyRequest"]
