# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""How a refusal is classified (OMN-17427)."""

from __future__ import annotations

from enum import StrEnum


class EnumRefusalClass(StrEnum):
    """capacity: no host had a free slot. policy: a rule refused the lane. limit: a usage limit
    or an expired login. other: everything else. none: the lane was not refused."""

    NONE = "none"
    CAPACITY = "capacity"
    POLICY = "policy"
    LIMIT = "limit"
    OTHER = "other"
