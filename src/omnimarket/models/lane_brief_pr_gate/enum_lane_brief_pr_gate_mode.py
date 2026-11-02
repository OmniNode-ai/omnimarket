# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The two modes of the brief gate's ``Opens PR in`` check."""

from __future__ import annotations

from enum import StrEnum


class EnumLaneBriefPrGateMode(StrEnum):
    """``observe`` reports a brief missing its line; ``enforce`` refuses it."""

    OBSERVE = "observe"
    ENFORCE = "enforce"
