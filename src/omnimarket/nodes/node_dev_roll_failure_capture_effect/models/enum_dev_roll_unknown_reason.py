# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The typed reason a terminal is unknown."""

from enum import StrEnum


class EnumDevRollUnknownReason(StrEnum):
    """Why the terminal is unknown."""

    INCIDENT_UNREADABLE = "incident_unreadable"
    ISSUER_MISSING = "issuer_missing"
    ARTIFACT_STORE_UNWRITABLE = "artifact_store_unwritable"
    OVERLAY_UNREADABLE = "overlay_unreadable"
