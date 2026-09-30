# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Authorized execution audiences for DoD evidence verification."""

from __future__ import annotations

from enum import StrEnum, unique


@unique
class EnumDodVerifyExecutionAudience(StrEnum):
    """Which verifier boundary is authorized to execute the invocation."""

    HOSTED = "hosted"
    LOCAL_DONE_GATE = "local_done_gate"


__all__ = ["EnumDodVerifyExecutionAudience"]
