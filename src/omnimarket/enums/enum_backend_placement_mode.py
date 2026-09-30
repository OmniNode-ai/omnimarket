# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""How a lane-added delegation backend shares work with the rungs it mirrors.

OMN-19215. A placement mirrors an added backend into a routing tier behind
named rungs. The mode decides whether the mirror only catches the rung's
failures or also shares the rung's first-choice traffic.
"""

from __future__ import annotations

from enum import StrEnum, unique


@unique
class EnumBackendPlacementMode(StrEnum):
    """Placement mode of a lane-added delegation backend."""

    FALLBACK = "fallback"
    """Offered only when the rung is unroutable or already tried."""

    SPREAD = "spread"
    """Shares first-choice traffic with the rung.

    Each request picks one member of the rung's group by a stable hash of its
    correlation id. A transport failure on the member picked first still
    retries the other members, as with ``FALLBACK``.
    """


__all__: list[str] = ["EnumBackendPlacementMode"]
