# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The session's omniclaude_mode, as plugins/onex/lib/mode.sh reports it (OMN-21795)."""

from enum import StrEnum


class EnumGuardSessionMode(StrEnum):
    """FULL inside the OmniNode workspace; LITE for a non-OmniNode install, which has no dev runtime."""

    FULL = "full"
    LITE = "lite"
