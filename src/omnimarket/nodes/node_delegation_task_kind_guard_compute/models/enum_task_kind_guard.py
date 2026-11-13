# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

from __future__ import annotations

from enum import StrEnum


class EnumTaskKindGuardRoute(StrEnum):
    LOCAL = "local"
    ESCALATE = "escalate"


class EnumTaskKindGuardReason(StrEnum):
    LOCAL_ACCEPTED = "local_accepted"
    KIND_STAYS_ON_CURRENT = "kind_stays_on_current"
    KIND_UNDECLARED = "kind_undeclared"
    DETERMINISTIC_DISAGREES = "deterministic_disagrees"
    GUARD_TRIPPED = "guard_tripped"


class EnumTaskKindGuardRefusal(StrEnum):
    OVERLAY_INVALID = "overlay_invalid"
    OVERLAY_UNBOUND = "overlay_unbound"
    NO_ROUTE = "no_route"


__all__: list[str] = [
    "EnumTaskKindGuardReason",
    "EnumTaskKindGuardRefusal",
    "EnumTaskKindGuardRoute",
]
