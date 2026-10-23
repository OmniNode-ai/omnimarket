# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Capture states derived from source and principal newest rows (OMN-21340)."""

from enum import StrEnum


class EnumCaptureWatchState(StrEnum):
    """UNKNOWN is an observation failure; SILENT and STALE ask for Slack."""

    FRESH = "FRESH"
    SILENT = "SILENT"
    STALE = "STALE"
    UNKNOWN = "UNKNOWN"
