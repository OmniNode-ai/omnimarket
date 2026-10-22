# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""How the operator is reaching a Claude Code session (OMN-20892)."""

from __future__ import annotations

from enum import StrEnum


class EnumAccessMode(StrEnum):
    """LOCAL: an interactive CLI at the Mac. REMOTE: a phone, web or Remote Control client.

    HEADLESS: a scripted session (``claude -p``, a landing worker) that no operator reads; the
    operator-facing rules never apply to it. An operator-facing session whose client cannot be
    told is resolved to REMOTE, the safer side.
    """

    LOCAL = "local"
    REMOTE = "remote"
    HEADLESS = "headless"
