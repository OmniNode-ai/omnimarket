# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Finding codes of the depends-on overlay check node (OMN-24537); each is a rule id of the core validator."""

from __future__ import annotations

from enum import StrEnum


class EnumDependsOnFindingCode(StrEnum):
    DEPENDENCY_ABSENT = "dependency_absent"
    DEPENDENCY_MALFORMED = "dependency_malformed"
    DEPENDENCY_KIND_UNKNOWN = "dependency_kind_unknown"
    FACTS_UNSUPPLIED = "facts_unsupplied"
