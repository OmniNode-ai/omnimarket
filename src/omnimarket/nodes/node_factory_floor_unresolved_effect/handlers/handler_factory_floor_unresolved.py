# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read the unresolved ledger evidence older than the floor's window, in parallel, for the floor to read (OMN-19998).

Measured on the Mac 2026-11-10 (cProfile, three runs each): four unresolved reads in series took 207 s of the factory floor
step's 231 s. They are this step's now; the floor reads the record this step writes and never the ledger for them.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from omnimarket.nodes.node_factory_floor_effect.models.model_unresolved_rows_record import (
    ModelUnresolvedRowsRecord,
)
from omnimarket.nodes.node_factory_floor_effect.source_loader import (
    OWN_CONTRACT_PATH,
    load_contract,
    read_unresolved_rows,
)
from omnimarket.nodes.node_factory_floor_unresolved_effect.models.model_factory_floor_unresolved import (
    ModelFactoryFloorUnresolvedOverlay,
    ModelFactoryFloorUnresolvedRequest,
)


class HandlerFactoryFloorUnresolved:
    """One pass of the unresolved reads under the overlay's budgets; the window, kinds, cut and plugin variable are the floor's own."""

    def __init__(
        self,
        overlay: ModelFactoryFloorUnresolvedOverlay,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._overlay = overlay
        self._clock = clock or (lambda: datetime.now(UTC))

    def handle(
        self, request: ModelFactoryFloorUnresolvedRequest
    ) -> ModelUnresolvedRowsRecord:
        runtime = load_contract(OWN_CONTRACT_PATH).runtime
        covers_before = (
            request.read_at
            - timedelta(days=runtime.ledger_window_days)
            + timedelta(hours=self._overlay.overlap_hours)
        )
        started = self._clock()
        rows = read_unresolved_rows(
            reads=tuple((read.kind, read.grep) for read in self._overlay.reads),
            cut_chars=runtime.ledger_cut_chars,
            timeout_seconds=self._overlay.read_timeout_seconds,
            plugin_root=Path(request.plugin_root) if request.plugin_root else None,
            plugin_root_env=runtime.plugin_root_env,
            stand_in_file=Path(request.stand_in_file)
            if request.stand_in_file
            else None,
            kinds=frozenset(runtime.ledger_kinds),
            older_than=covers_before,
            max_workers=self._overlay.read_workers,
        )
        return ModelUnresolvedRowsRecord(
            read_at=request.read_at,
            covers_before=covers_before,
            valid_hours=self._overlay.overlap_hours,
            seconds=max(0.0, (self._clock() - started).total_seconds()),
            rows=rows,
        )
