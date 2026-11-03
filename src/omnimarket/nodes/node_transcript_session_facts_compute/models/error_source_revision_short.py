# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Error raised when a transcript read falls short of the required revision."""

from __future__ import annotations


class SourceRevisionShortError(ValueError):
    """The supplied transcript has not reached the required source revision."""
