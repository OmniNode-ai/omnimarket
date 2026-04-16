# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler for `node_ledger_append_effect` (OMN-8948).

EFFECT node: appends a tick line to a journal file (real observable side-effect),
emits a `ModelLedgerAppendedEvent` carrying the line number for downstream.

Per ONEX rules: EFFECT emits events (never returns a result, never emits intents
or projections). State-root lookup uses the TEST-ONLY `ONEX_STATE_ROOT` env var
documented at runtime_local.py (set per-run by RuntimeLocal).
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
from uuid import uuid4

from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput

from omnimarket.nodes.node_ledger_append_effect.models.model_ledger_appended_event import (
    ModelLedgerAppendedEvent,
)
from omnimarket.nodes.node_ledger_orchestrator.models.model_ledger_tick_command import (
    ModelLedgerAppendCommand,
)

JOURNAL_FILENAME = "ledger-journal.txt"


def _journal_path() -> Path:
    """Return the active journal file path under ONEX_STATE_ROOT."""
    return Path(os.environ["ONEX_STATE_ROOT"]) / JOURNAL_FILENAME


class HandlerLedgerAppend:
    """Writes a tick line to the journal and emits the appended event."""

    def handle(self, request: ModelLedgerAppendCommand) -> ModelHandlerOutput[None]:
        journal = _journal_path()
        journal.parent.mkdir(parents=True, exist_ok=True)

        line_content = f"{request.tick_id}"
        # flock serializes append+count so concurrent handle() calls can't
        # interleave write and recount, producing duplicate line_number values.
        with journal.open("a+", encoding="utf-8") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                fh.write(line_content + "\n")
                fh.flush()
                fh.seek(0)
                line_number = sum(1 for _ in fh)
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

        appended = ModelLedgerAppendedEvent(
            tick_id=request.tick_id,
            correlation_id=request.correlation_id,
            line_number=line_number,
            line_content=line_content,
        )
        return ModelHandlerOutput.for_effect(
            input_envelope_id=uuid4(),
            correlation_id=request.correlation_id,
            handler_id="node_ledger_append_effect",
            events=(appended,),
        )
