# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Outcome of one context-pack render (OMN-21109)."""

from enum import StrEnum


class EnumContextPackRenderStatus(StrEnum):
    """OK carries messages and hashes; FAILED carries a typed failure class and no messages."""

    OK = "ok"
    FAILED = "failed"
