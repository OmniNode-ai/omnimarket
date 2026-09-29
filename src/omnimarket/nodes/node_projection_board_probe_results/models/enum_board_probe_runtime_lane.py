"""Runtime attachment scope for the board probe-results projection."""

from enum import StrEnum


class EnumBoardProbeRuntimeLane(StrEnum):
    """The same three runtime lanes used by the sibling projection."""

    COMPOSE_DEV = "compose-dev"
    ONEX_LAB = "onex-lab"
    ONEX_LAB_K3S = "onex-lab-k3s"


__all__ = ["EnumBoardProbeRuntimeLane"]
