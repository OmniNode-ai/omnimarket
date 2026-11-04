# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Journal an additive call's miss record in the hook emit journal (OMN-22016, TC.11).

The record is the profile, the reason and the elapsed time and nothing else, in the same atomic
envelope the session barrier uses; the resident drainer delivers it under the ``additive.miss``
event type, which the emit registry declares as owned by this node.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from omnimarket.models.claude_code_binding import ModelAdditiveMissRecord
from omnimarket.nodes.node_claude_code_hook_adapter_effect.handlers.handler_session_barrier_journal import (
    JOURNAL_DIR_ENV,
    STATE_DIR_ENV,
)

MISS_EVENT_TYPE = "additive.miss"


class AdditiveMissJournal:
    """journal(record, correlation_id) writes one envelope file; nothing else is written."""

    def __init__(
        self,
        *,
        environ: Mapping[str, str] | None = None,
        time_ns: Callable[[], int] = time.time_ns,
    ) -> None:
        self._environ = os.environ if environ is None else environ
        self._time_ns = time_ns

    def journal(self, record: ModelAdditiveMissRecord, correlation_id: str) -> None:
        journal_dir = self._journal_dir()
        journal_dir.mkdir(parents=True, exist_ok=True)
        event_id = str(uuid4())
        name = f"{self._time_ns():020d}_{event_id}.json"
        temporary = journal_dir / f".{name}.tmp"
        temporary.write_text(
            json.dumps(
                {
                    "event_id": event_id,
                    "event_type": MISS_EVENT_TYPE,
                    "payload": record.model_dump(mode="json"),
                    "correlation_id": correlation_id,
                    "queued_at": datetime.now(UTC).isoformat(),
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        os.replace(temporary, journal_dir / name)

    def _journal_dir(self) -> Path:
        if directory := self._environ.get(JOURNAL_DIR_ENV):
            return Path(directory)
        if state_dir := self._environ.get(STATE_DIR_ENV):
            return Path(state_dir) / "hook_emit_journal"
        return Path.home() / ".onex_state" / "hook_emit_journal"
