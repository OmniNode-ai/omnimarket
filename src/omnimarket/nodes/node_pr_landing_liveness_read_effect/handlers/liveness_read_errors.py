# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The liveness read node's one error: the read could not be made (OMN-23529)."""

from __future__ import annotations


class LivenessReadError(Exception):
    """The read could not be made; the message says why, in one line."""


__all__ = ["LivenessReadError"]
