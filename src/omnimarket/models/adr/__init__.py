# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Shared ADR pipeline types — used across ADR canary nodes.

These types are promoted here so no node needs to import private models from
another node's package. Each sub-node defines its own request/result types
internally; these shared types carry data the orchestrator knows about.
"""

from omnimarket.models.adr.model_adr_pipeline import (
    EnumAdrKBDestination,
    EnumAdrPublicationClassification,
    EnumAdrSourceVisibility,
    ModelAdrDocumentRef,
    ModelAdrDocumentSegment,
    ModelAdrExtractionSummary,
    ModelAdrGradingScores,
    ModelAdrIngestionResult,
    ModelAdrManifestEntry,
    ModelAdrManifestModel,
    ModelAdrPublicationCandidate,
    ModelAdrRunRequest,
    ModelAdrSegmentationResult,
    ModelAdrSourceDocumentEvidence,
    ModelAdrSourceProvenance,
)

__all__ = [
    "EnumAdrKBDestination",
    "EnumAdrPublicationClassification",
    "EnumAdrSourceVisibility",
    "ModelAdrDocumentRef",
    "ModelAdrDocumentSegment",
    "ModelAdrExtractionSummary",
    "ModelAdrGradingScores",
    "ModelAdrIngestionResult",
    "ModelAdrManifestEntry",
    "ModelAdrManifestModel",
    "ModelAdrPublicationCandidate",
    "ModelAdrRunRequest",
    "ModelAdrSegmentationResult",
    "ModelAdrSourceDocumentEvidence",
    "ModelAdrSourceProvenance",
]
