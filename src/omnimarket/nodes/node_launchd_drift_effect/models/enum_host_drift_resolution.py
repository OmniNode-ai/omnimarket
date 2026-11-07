# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Host drift resolution."""

from __future__ import annotations

from enum import StrEnum


class EnumHostDriftResolution(StrEnum):
    OBSERVED_CONVERGED = "OBSERVED_CONVERGED"
    AUTO_APPLIED = "AUTO_APPLIED"
