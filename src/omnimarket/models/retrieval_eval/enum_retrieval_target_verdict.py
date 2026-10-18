# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Overall target verdict identifiers (OMN-21132, plan task TC.9, port item AP-E1)."""

from enum import StrEnum


class EnumRetrievalTargetVerdict(StrEnum):
    """Whether the overall mean meets the configured target."""

    PASS = "pass"
    FAIL = "fail"
