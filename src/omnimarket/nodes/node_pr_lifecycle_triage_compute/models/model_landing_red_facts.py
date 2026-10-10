# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One head's red check names and runs, for the landing red class."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelLandingRedFacts(BaseModel):
    """``runs`` are the watcher rows for this head; ``annotations`` is None unless the caller reads them
    (cause mode); ``cancelled`` is the head's cancelled check copies, passed only under a stale-refresh node."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    red: tuple[str, ...]
    runs: tuple[tuple[str | None, ...], ...] = ()
    companion_merged: bool
    edge_fired: bool = False
    annotations: dict[str, str | None] | None = None
    cancelled: tuple[str, ...] = ()


__all__: list[str] = ["ModelLandingRedFacts"]
