# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models of the pull-request test selector reading (OMN-22951).

A selector (the ``detect-changes`` job and its ``ENABLE_SMART_TESTS`` flag) exists in
several repositories. Its flag is unreadable from the watcher, so this reading takes the
three things a reader can observe per repository, the workflow file, the repository's
Actions variable and branch protection, and turns them into one state per repository,
plus a count of shadow misses: failures the same-head full suite found that the narrowed
run did not.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

# The four repositories whose ci.yml carries the selector (config control plane plan,
# 2026-08-24: "read across four repos' ci.yml"). A request may name another set.
SELECTOR_REPOSITORIES: tuple[str, ...] = (
    "omnibase_core",
    "omnibase_infra",
    "omniclaude",
    "omnimarket",
)
SELECTOR_FLAG_NAME = "ENABLE_SMART_TESTS"


class EnumSelectorReadingState(StrEnum):
    """WORKING, NOT WORKING or UNKNOWN. Anything but WORKING is red."""

    WORKING = "WORKING"
    NOT_WORKING = "NOT WORKING"
    UNKNOWN = "UNKNOWN"


class EnumSelectorActivation(StrEnum):
    """Whether the selector narrows pull-request runs in one repository."""

    ACTIVE = "active"
    DISABLED = "disabled"
    UNREADABLE = "unreadable"


class ModelSelectorShadowComparison(BaseModel):
    """One pull request's narrowed run beside the full suite on the same head."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pr: str
    selected_head_sha: str
    full_suite_head_sha: str
    selected_failures: tuple[str, ...] = ()
    full_suite_failures: tuple[str, ...] = ()


class ModelSelectorRepoObservation(BaseModel):
    """What a reader saw in one repository. ``None`` means that source was unreadable."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str
    workflow_text: str | None = None
    flag_value: str | None = None
    flag_readable: bool = False
    required_contexts: tuple[str, ...] | None = None
    selector_contexts: tuple[str, ...] = ("Detect Changes",)
    comparisons: tuple[ModelSelectorShadowComparison, ...] = ()


class ModelTestSelectorReadingRequest(BaseModel):
    """The observations and the time they are read at."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    observations: tuple[ModelSelectorRepoObservation, ...]
    expected_repos: tuple[str, ...] = SELECTOR_REPOSITORIES
    now: datetime


class ModelSelectorRepoReading(BaseModel):
    """One repository's activation and shadow-miss count."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str
    state: EnumSelectorReadingState
    activation: EnumSelectorActivation
    workflow_references_flag: bool | None
    flag_enabled: bool | None
    protection_requires_selector: bool | None
    matched_comparisons: int = Field(ge=0)
    unmatched_comparisons: int = Field(ge=0)
    missed_failures: tuple[str, ...]
    shadow_misses: int = Field(ge=0)
    reason: str


class ModelTestSelectorReading(BaseModel):
    """WORKING only when every expected repository is active with no shadow miss."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: EnumSelectorReadingState
    shadow_misses: int = Field(ge=0)
    read_at: datetime
    repos: tuple[ModelSelectorRepoReading, ...]
    reason: str
