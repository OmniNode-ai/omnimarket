# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
from __future__ import annotations

from enum import StrEnum


class EnumToolOutcome(StrEnum):
    OK = "ok"
    FAILED = "failed"
    REFUSED = "refused"
    TIMEOUT = "timeout"
