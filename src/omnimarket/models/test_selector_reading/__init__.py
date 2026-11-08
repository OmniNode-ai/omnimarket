# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared models and the pure reading rules of the test selector reading."""

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
from omnimarket.models.test_selector_reading.reading_rules import (
    read_repo,
    read_selector,
)

__all__: list[str] = [
    "SELECTOR_FLAG_NAME",
    "SELECTOR_REPOSITORIES",
    "EnumSelectorActivation",
    "EnumSelectorReadingState",
    "ModelSelectorRepoObservation",
    "ModelSelectorRepoReading",
    "ModelSelectorShadowComparison",
    "ModelTestSelectorReading",
    "ModelTestSelectorReadingRequest",
    "read_repo",
    "read_selector",
]
