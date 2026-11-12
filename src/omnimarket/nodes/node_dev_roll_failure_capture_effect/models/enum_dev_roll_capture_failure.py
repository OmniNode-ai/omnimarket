# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The typed reasons a dev-roll capture fails (OMN-24013, five whys R2)."""

from __future__ import annotations

from enum import Enum


class EnumDevRollCaptureFailure(str, Enum):
    """Why the terminal record or manifest could not be written."""

    STORE_UNWRITABLE = "store_unwritable"
    INCIDENT_UNREADABLE = "incident_unreadable"
