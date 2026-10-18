# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The closed verdict set of one experiment."""

from __future__ import annotations

from enum import StrEnum


class EnumExperimentVerdict(StrEnum):
    """A void verdict carries its cause on the record."""

    SUPPORTED = "supported"
    REFUTED = "refuted"
    VOID = "void"
