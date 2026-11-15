# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The injected seam: how the installed units, ticks and writers' sources are read. In memory by default; the file reader is the real backend, in node_one_copy_check_effect."""

from __future__ import annotations

from collections.abc import Sequence
from enum import Enum
from typing import Protocol

from omnimarket.nodes.node_one_copy_check_compute.models import ModelReferrer


class ReferrerReadError(RuntimeError):
    """A referrer could not be read; the check is unknown, never clean."""


class EnumReferrerBreak(Enum):
    """The named injected breaks of the reading, each one a way the real read goes wrong."""

    REFERRERS_UNREADABLE = "referrers_unreadable"
    REFERRERS_SILENT = "referrers_silent"


class ProtocolReferrerReader(Protocol):
    def read(self) -> tuple[ModelReferrer, ...]: ...


class InMemoryReferrerReader:
    """The default reader: referrers held in memory, with an optional named injected break."""

    def __init__(
        self,
        referrers: Sequence[ModelReferrer] = (),
        break_: EnumReferrerBreak | None = None,
    ) -> None:
        self._referrers = tuple(referrers)
        self._break = break_

    def read(self) -> tuple[ModelReferrer, ...]:
        if self._break is EnumReferrerBreak.REFERRERS_UNREADABLE:
            raise ReferrerReadError("injected break: the referrers cannot be read")
        if self._break is EnumReferrerBreak.REFERRERS_SILENT:
            return ()
        return self._referrers
