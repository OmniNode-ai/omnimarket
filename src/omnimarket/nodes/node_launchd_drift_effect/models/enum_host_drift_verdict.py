# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Host drift verdict."""

from __future__ import annotations

from enum import StrEnum


class EnumHostDriftVerdict(StrEnum):
    OK = "OK"
    DRIFT = "DRIFT"
    CHECK_FAILED = "CHECK_FAILED"
