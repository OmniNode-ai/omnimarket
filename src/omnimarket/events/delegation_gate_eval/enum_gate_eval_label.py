# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Values for EnumGateEvalLabel."""

from enum import StrEnum


class EnumGateEvalLabel(StrEnum):
    ADEQUATE = "adequate"
    INADEQUATE = "inadequate"
    UNLABELABLE = "unlabelable"
