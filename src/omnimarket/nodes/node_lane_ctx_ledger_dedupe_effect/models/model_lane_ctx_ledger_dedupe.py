# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed command, events and result of the lane context ledger archive dedupe (OMN-23297)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelLaneCtxLedgerDedupeCommand(_Frozen):
    """One pass over the lane context directories of this host.

    ``runs_root`` holds the ``<run>.ctx`` context directories; ``store_dir`` is the host's one copy
    of each archive roll and must sit on the same filesystem. Both come from the node's overlay.
    A roll modified within ``settle_seconds`` is left alone, so a roll still being unpacked is
    never replaced. ``execute`` False reads what would change and changes nothing.
    """

    runs_root: str = Field(min_length=1)
    store_dir: str = Field(min_length=1)
    settle_seconds: int = Field(ge=0)
    execute: bool = True


class ModelLaneCtxLedgerDedupeEvent(_Frozen):
    """A published fact: a roll that could not be shared (refused topic)."""

    roll: str
    reason: str


class ModelLaneCtxLedgerDedupeResult(_Frozen):
    """What one pass did. ``errors`` empty means every roll seen was shared, kept or skipped on purpose."""

    mode: Literal["execute", "dry-run"]
    contexts: int = 0
    rolls_seen: int = 0
    stored: int = 0
    already_shared: int = 0
    linked: int = 0
    kept_own: int = 0
    unsettled: int = 0
    bytes_freed: int = 0
    errors: tuple[str, ...] = ()
    kept_own_rolls: tuple[str, ...] = ()

    @property
    def exit_code(self) -> int:
        return 1 if self.errors else 0

    def line(self) -> str:
        verb = "linked" if self.mode == "execute" else "would_link"
        freed = "bytes_freed" if self.mode == "execute" else "would_free_bytes"
        return (
            f"LEDGER-DEDUPE mode={self.mode} contexts={self.contexts} rolls={self.rolls_seen} "
            f"stored={self.stored} already_shared={self.already_shared} {verb}={self.linked} "
            f"{freed}={self.bytes_freed} kept_own={self.kept_own} unsettled={self.unsettled} "
            f"errors={len(self.errors)}"
        )
