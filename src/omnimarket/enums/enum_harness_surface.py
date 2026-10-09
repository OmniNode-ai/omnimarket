# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Surface admitted for house harness execution."""

from __future__ import annotations

from enum import StrEnum, unique


@unique
class EnumHarnessSurface(StrEnum):
    """Surface admitted for house harness execution."""

    INTERNAL = "internal"


__all__: list[str] = ["EnumHarnessSurface"]
