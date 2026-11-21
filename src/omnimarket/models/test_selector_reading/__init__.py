# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared models and the pure reading rules of the test selector reading."""

from omnimarket.models.test_selector_reading.model_nightly_attribution import (
    ATTRIBUTION_LIMITS,
    EnumAttributionClass,
    EnumEvidenceState,
    EnumFileOutcome,
    EnumIncompleteReason,
    EnumShardConclusion,
    EnumUnattributableReason,
    ModelAttributionCandidate,
    ModelCandidateEvidence,
    ModelCandidateRecord,
    ModelCandidateRun,
    ModelCandidateSelection,
    ModelFileAttribution,
    ModelNightCompleteness,
    ModelNightlyAttribution,
    ModelNightlyAttributionRequest,
)
from omnimarket.models.test_selector_reading.model_test_selector_reading import (
    SELECTOR_FLAG_NAME,
    SELECTOR_REPOSITORIES,
    EnumSelectorActivation,
    EnumSelectorReadingState,
    ModelSelectorRepoObservation,
    ModelSelectorRepoReading,
    ModelSelectorShadowComparison,
    ModelTestSelectorReading,
    ModelTestSelectorReadingRequest,
)
from omnimarket.models.test_selector_reading.nightly_attribution_rules import (
    attribute_file,
    attribute_nightly_failures,
    candidate_evidence,
    incomplete_reasons,
    selects,
)
from omnimarket.models.test_selector_reading.reading_rules import (
    read_repo,
    read_selector,
)

__all__: list[str] = [
    "ATTRIBUTION_LIMITS",
    "SELECTOR_FLAG_NAME",
    "SELECTOR_REPOSITORIES",
    "EnumAttributionClass",
    "EnumEvidenceState",
    "EnumFileOutcome",
    "EnumIncompleteReason",
    "EnumSelectorActivation",
    "EnumSelectorReadingState",
    "EnumShardConclusion",
    "EnumUnattributableReason",
    "ModelAttributionCandidate",
    "ModelCandidateEvidence",
    "ModelCandidateRecord",
    "ModelCandidateRun",
    "ModelCandidateSelection",
    "ModelFileAttribution",
    "ModelNightCompleteness",
    "ModelNightlyAttribution",
    "ModelNightlyAttributionRequest",
    "ModelSelectorRepoObservation",
    "ModelSelectorRepoReading",
    "ModelSelectorShadowComparison",
    "ModelTestSelectorReading",
    "ModelTestSelectorReadingRequest",
    "attribute_file",
    "attribute_nightly_failures",
    "candidate_evidence",
    "incomplete_reasons",
    "read_repo",
    "read_selector",
    "selects",
]
