# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Journal the session barrier before publishing compaction (OMN-21099, TA.21).

The adapter journals a barrier for each capture stream before publishing, so
the resident drainer delivers it on the content stream behind every record
journalled before it. The capture identity contract owns the per-session
sequence high-waters and the barrier payloads; this adapter owns their atomic
hook emit journal envelopes.
"""

from __future__ import annotations

import importlib
import json
import os
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from uuid import UUID, uuid4

import yaml
from pydantic import JsonValue

import omnimarket.nodes

JOURNAL_DIR_ENV = "ONEX_HOOK_EMIT_JOURNAL_DIR"
STATE_DIR_ENV = "ONEX_STATE_DIR"
CONTENT_CAPTURE_ENV = "OMNICLAUDE_CONTENT_CAPTURE"
BARRIER_EVENT_TYPE = "content.captured"

_sequence_lock = Lock()
_last_sequence = 0


def _next_sequence(time_ns: Callable[[], int]) -> int:
    """Keep file names increasing within this process, even if time goes back."""
    global _last_sequence
    with _sequence_lock:
        _last_sequence = max(time_ns(), _last_sequence + 1)
        return _last_sequence


class HandlerSessionBarrierJournal:
    """journal(session_id) -> the barrier id carried on the compaction command."""

    def __init__(
        self,
        *,
        environ: Mapping[str, str] | None = None,
        barrier_ids: Callable[[], UUID] = uuid4,
        time_ns: Callable[[], int] = time.time_ns,
    ) -> None:
        self._environ = os.environ if environ is None else environ
        self._barrier_ids = barrier_ids
        self._time_ns = time_ns

    def journal(self, session_id: str) -> str:
        if not session_id:
            raise ValueError("session barrier needs a non-empty session_id")
        journal_dir = self._journal_dir()
        enabled = self._environ.get(CONTENT_CAPTURE_ENV, "").strip().lower() not in {
            "0",
            "off",
            "false",
            "no",
        }
        streams = ("lineage", "content") if enabled else ("lineage",)
        barrier_id = str(self._barrier_ids())
        contract_path = (
            Path(next(iter(omnimarket.nodes.__path__)))
            / "node_capture_item_identity_effect"
            / "contract.yaml"
        )
        contract = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
        handler_spec = contract["handler"]
        handler = getattr(
            importlib.import_module(handler_spec["module"]), handler_spec["class"]
        )()
        model_module, _, model_name = contract["input_model"].rpartition(".")
        request = getattr(importlib.import_module(model_module), model_name)(
            operation="session_barrier",
            journal_dir=str(journal_dir),
            session_id=session_id,
            streams=list(streams),
            content_capture_enabled=enabled,
            barrier_id=barrier_id,
        )
        records: list[dict[str, JsonValue]] = handler.handle(request).records
        journal_dir.mkdir(parents=True, exist_ok=True)
        for record in records:
            event_id = str(uuid4())
            name = f"{_next_sequence(self._time_ns):020d}_{event_id}.json"
            temporary = journal_dir / f".{name}.tmp"
            temporary.write_text(
                json.dumps(
                    {
                        "event_id": event_id,
                        "event_type": BARRIER_EVENT_TYPE,
                        "payload": record,
                        "correlation_id": session_id,
                        "queued_at": datetime.now(UTC).isoformat(),
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            os.replace(temporary, journal_dir / name)
        return barrier_id

    def _journal_dir(self) -> Path:
        if directory := self._environ.get(JOURNAL_DIR_ENV):
            return Path(directory)
        if state_dir := self._environ.get(STATE_DIR_ENV):
            return Path(state_dir) / "hook_emit_journal"
        return Path.home() / ".onex_state" / "hook_emit_journal"
