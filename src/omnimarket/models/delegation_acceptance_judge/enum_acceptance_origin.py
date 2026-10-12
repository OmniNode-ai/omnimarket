# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Where a judged verdict came from: delegation traffic or an eval run."""

from __future__ import annotations

from enum import StrEnum


class EnumAcceptanceOrigin(StrEnum):
    """Closed set. The fold keeps the two apart and never pools them."""

    LIVE = "live"
    EVAL = "eval"
