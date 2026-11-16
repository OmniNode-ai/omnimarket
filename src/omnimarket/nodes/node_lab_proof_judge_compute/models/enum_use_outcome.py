# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""EnumUseOutcome: the outcome of a not-applicable use."""

from __future__ import annotations

from enum import StrEnum


class EnumUseOutcome(StrEnum):
    """The outcome of a not-applicable use."""

    NOT_APPLICABLE = "not_applicable"
    REFUSED_NO_SUBSTITUTE = "refused_no_substitute"
