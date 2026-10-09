# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Reasons a declared harness rung cannot be selected."""

from __future__ import annotations

from enum import StrEnum, unique


@unique
class EnumHarnessRungRefusal(StrEnum):
    """Reasons a declared harness rung cannot be selected."""

    EXECUTOR_UNBOUND = "executor_unbound"
    NOT_HOUSE_TENANT = "not_house_tenant"
    NOT_INTERNAL_SURFACE = "not_internal_surface"


__all__: list[str] = ["EnumHarnessRungRefusal"]
