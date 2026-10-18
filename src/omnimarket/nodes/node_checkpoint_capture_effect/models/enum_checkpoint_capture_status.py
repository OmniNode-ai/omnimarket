# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What one checkpoint capture did."""

from __future__ import annotations

from enum import StrEnum


class EnumCheckpointCaptureStatus(StrEnum):
    """ok: the capture exited 0; skipped: no usable entry; failed: anything else."""

    OK = "ok"
    SKIPPED = "skipped"
    FAILED = "failed"


__all__ = ["EnumCheckpointCaptureStatus"]
