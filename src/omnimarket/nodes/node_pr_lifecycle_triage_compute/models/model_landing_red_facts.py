# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One head's red check names and runs, for the landing red class."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelLandingRedFacts(BaseModel):
    """``runs`` are the watcher rows for this head; ``annotations`` is None unless the caller reads them
    (cause mode); ``cancelled`` is the head's cancelled check copies, passed only under a stale-refresh node.
    ``failure_logs`` maps a check name to that failing job's log text; ``online_runner_labels`` is one tuple of
    labels per runner that fleet-status reads ONLINE (None means fleet-status was not read)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    red: tuple[str, ...]
    runs: tuple[tuple[str | None, ...], ...] = ()
    companion_merged: bool
    edge_fired: bool = False
    annotations: dict[str, str | None] | None = None
    cancelled: tuple[str, ...] = ()
    failure_logs: dict[str, str | None] | None = None
    online_runner_labels: tuple[tuple[str, ...], ...] | None = None


__all__: list[str] = ["ModelLandingRedFacts"]
