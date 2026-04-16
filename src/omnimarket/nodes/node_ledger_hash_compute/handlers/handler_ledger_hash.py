# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler for `node_ledger_hash_compute` (OMN-8949).

COMPUTE node: read-only deterministic over the journal file artifact.
Returns sha256 + line_count. No writes, no bus publishes.

Per ONEX rules: COMPUTE returns `result` (required). Forbidden: events,
intents, projections. Purity note: this handler is pure in the "no side
effects" sense — it reads the journal file produced by the effect, but the
compute itself does not mutate state. The external file is treated as
deterministic input.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from omnimarket.nodes.node_ledger_append_effect.models.model_ledger_appended_event import (
    ModelLedgerAppendedEvent,
)
from omnimarket.nodes.node_ledger_hash_compute.models.model_ledger_hash_computed import (
    ModelLedgerHashComputed,
)

JOURNAL_FILENAME = "ledger-journal.txt"


class HandlerLedgerHashCompute:
    """Reads the journal, computes sha256 + line count, returns the result."""

    def handle(self, request: ModelLedgerAppendedEvent) -> ModelLedgerHashComputed:
        journal = Path(os.environ["ONEX_STATE_ROOT"]) / JOURNAL_FILENAME
        content_bytes = journal.read_bytes()
        line_count = content_bytes.count(b"\n")
        digest = hashlib.sha256(content_bytes).hexdigest()
        return ModelLedgerHashComputed(
            tick_id=request.tick_id,
            correlation_id=request.correlation_id,
            line_count=line_count,
            sha256_hex=digest,
        )
