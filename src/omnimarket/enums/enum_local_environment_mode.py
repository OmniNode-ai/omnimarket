# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Which local mode an environment report describes."""

from __future__ import annotations

from enum import StrEnum, unique


@unique
class EnumLocalEnvironmentMode(StrEnum):
    """Native is `onex dashboard` with no stack; compose is the catalog local bundle or a lab lane."""

    NATIVE = "native"
    COMPOSE = "compose"


__all__ = ["EnumLocalEnvironmentMode"]
