# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Defect catalog families, entry attributes and resolution outcomes."""

from __future__ import annotations

from enum import StrEnum, unique


@unique
class EnumReviewDefectFamily(StrEnum):
    """The eight defect families available to the reviewer."""

    ARCHITECTURE = "architecture"
    SECURITY = "security"
    PERFORMANCE = "performance"
    CORRECTNESS = "correctness"
    COMPLETENESS = "completeness"
    FEASIBILITY = "feasibility"
    TESTING = "testing"
    STYLE = "style"


@unique
class EnumReviewDefectSeverity(StrEnum):
    """Default severity of a catalog defect."""

    CRITICAL = "critical"
    MAJOR = "major"
    MINOR = "minor"
    NIT = "nit"


@unique
class EnumReviewDefectCheckability(StrEnum):
    """How a defect can be checked."""

    MODEL_ONLY = "model-only"
    PATTERN = "pattern"
    AST = "ast"
    SCHEMA = "schema"


@unique
class EnumReviewDefectStatus(StrEnum):
    """Whether a code is available for new findings."""

    ACTIVE = "active"
    RETIRED = "retired"


@unique
class EnumReviewDefectCatalogOutcome(StrEnum):
    """Whether the catalog resolved into a usable review section."""

    RESOLVED = "resolved"
    REFUSED = "refused"


@unique
class EnumReviewDefectCatalogRefusal(StrEnum):
    """Why the resolver refused to provide a review section."""

    INVALID_CATALOG = "invalid_catalog"
    DUPLICATE_CODE = "duplicate_code"
    CODE_FAMILY_MISMATCH = "code_family_mismatch"
    ENFORCED_BY_WITHOUT_VERSION = "enforced_by_without_version"
    SECTION_OVER_BUDGET = "section_over_budget"
    VERSION_MISMATCH = "version_mismatch"
    UNKNOWN_CODE = "unknown_code"
