# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The needs= actions of the remote-lane closing row (OMN-23417)."""

from __future__ import annotations

from enum import StrEnum


class EnumLaneNeedsAction(StrEnum):
    """What a stopped lane asks someone else to do; the needs= cell of its closing row."""

    RERUN = "rerun"
    UPDATE_BRANCH = "update-branch"
    CLOSE = "close"
    INSTALL = "install"
    RULING = "ruling"
    ADMIN = "admin"
    TICKET = "ticket"
    RELEASE_RECUT = "release-recut"
