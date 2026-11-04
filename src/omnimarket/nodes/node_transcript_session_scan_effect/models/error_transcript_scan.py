# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Errors of the transcript scan, each naming the overlay key or root at fault."""

from __future__ import annotations


class TranscriptRootUnavailableError(ValueError):
    """The overlay names a root that no store was injected for, or the request names an unknown root."""


class TranscriptReadTooLargeError(ValueError):
    """An object is larger than the overlay's read_max_bytes."""


class TranscriptUndecodableError(ValueError):
    """An object's bytes are not UTF-8, so no source revision can be computed from its text."""
