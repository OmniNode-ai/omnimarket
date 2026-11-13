# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Terminal status vocabulary for the experiment result record compute node."""

from __future__ import annotations

from enum import StrEnum


class EnumExperimentResultRecordStatus(StrEnum):
    """The runtime's terminal vocabulary."""

    COMPLETED = "completed"
    FAILED = "failed"


__all__ = ["EnumExperimentResultRecordStatus"]
