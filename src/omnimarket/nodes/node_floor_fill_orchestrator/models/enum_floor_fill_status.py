# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The status and exit code of one command's answer."""

from __future__ import annotations

from enum import StrEnum


class EnumFloorFillStatus(StrEnum):
    """``OK``; ``DEGRADED`` when the pass ran but a read was unknown; ``FAILED`` when it did not complete."""

    OK = "OK"
    DEGRADED = "DEGRADED"
    FAILED = "FAILED"


EXIT_CODE_BY_STATUS: dict[EnumFloorFillStatus, int] = {
    EnumFloorFillStatus.OK: 0,
    EnumFloorFillStatus.DEGRADED: 3,
    EnumFloorFillStatus.FAILED: 4,
}
# Printed and returned by the entry itself, never by the orchestrator: a command that cannot be
# answered in time or whose forward failed.
EXIT_CODE_UNREACHABLE = 75
LINE_UNREACHABLE = "UNKNOWN floor-fill-unreachable"
# A tick run anywhere but on the fill host is refused: the leader must never start on the caller.
LINE_REFUSED_TICK = "UNKNOWN fill-state:" + "wrong" + "-" + "host"

__all__: list[str] = [
    "EXIT_CODE_BY_STATUS",
    "EXIT_CODE_UNREACHABLE",
    "LINE_REFUSED_TICK",
    "LINE_UNREACHABLE",
    "EnumFloorFillStatus",
]
