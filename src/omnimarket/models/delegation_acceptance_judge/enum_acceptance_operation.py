# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The four pure operations of the acceptance judge."""

from __future__ import annotations

from enum import StrEnum


class EnumAcceptanceOperation(StrEnum):
    """The four pure operations of the acceptance judge."""

    RENDER = "render"
    CHECK = "check"
    SCORE = "score"
    EVENTS = "events"
