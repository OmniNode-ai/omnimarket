"""Offline V4 artifact-evidence validation node."""

from .handlers.handler_v4_artifact_evidence import (
    HandlerV4ArtifactEvidence,
    validate_v4_artifact_evidence,
)


class NodeRsdV4ArtifactEvidenceValidateCompute(HandlerV4ArtifactEvidence):
    """ONEX wrapper for the effect-free V4 artifact verifier."""


__all__ = [
    "HandlerV4ArtifactEvidence",
    "NodeRsdV4ArtifactEvidenceValidateCompute",
    "validate_v4_artifact_evidence",
]
