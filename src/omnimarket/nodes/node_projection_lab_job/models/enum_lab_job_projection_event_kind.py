# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The one event the lab job projection consumes."""

from enum import StrEnum


class EnumLabJobProjectionEventKind(StrEnum):
    """The request field filled by the transitioned topic."""

    TRANSITIONED = "transitioned"


__all__: list[str] = ["EnumLabJobProjectionEventKind"]
