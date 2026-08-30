# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Durable per-item retry ledger for DoD verification (OMN-17022 / A15).

The effect boundary for :mod:`retry_policy`, which is pure. Same shape as the
OMN-17018 dispatch-queue lifecycle ledger one layer up: a Protocol plus a
filesystem implementation, injectable so callers and tests share one contract.

The record lives beside the evidence it explains —
``<ticket_state_root>/<TICKET-ID>/dod_retry_state.json`` — next to the
``dod_report.json`` both producers already write there, so an operator reading
a receipt finds the attempt history in the same directory rather than in a
separate store they have to know about.

``ticket_state_root`` is the directory that DIRECTLY contains the per-ticket
directories, and nothing else. The two producers reach it differently — the
sweep from its ``evidence_root`` plus ``.evidence``, the CLI straight from
``ONEX_EVIDENCE_ROOT`` — and a ledger that tried to infer which convention it
had been handed would be exactly the silent-default guessing this codebase
forbids. Each caller states it.

``write`` enforces append-only-ness **durably**, not merely in the model: a
fresh process that constructs an empty state for an already-recorded ticket is
refused rather than allowed to truncate the history back to a PENDING-reading
record. That is the arm of DoD 4 an in-memory validator cannot reach.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Protocol, runtime_checkable

from omnimarket.nodes.node_dod_verify.models.model_dod_verify_retry_state import (
    ModelDodVerifyRetryState,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_state import (
    EnumDodVerifyStatus,
)

logger = logging.getLogger(__name__)

_RETRY_STATE_FILENAME = "dod_retry_state.json"
_TICKET_RE = re.compile(r"^OMN-\d+$", re.IGNORECASE)


@runtime_checkable
class ProtocolDodVerifyRetryLedger(Protocol):
    """Durable per-item retry state, addressed by ticket id."""

    def read(self, ticket_id: str) -> ModelDodVerifyRetryState | None:
        """Return the recorded state, or None when the item has no record.

        None means "nothing has ever been written about this item" — it is NOT
        an empty state, because a caller that cannot tell those apart is the
        defect this ticket closes.
        """
        ...

    def write(self, state: ModelDodVerifyRetryState) -> None:
        """Persist ``state``, refusing any write that loses recorded history."""
        ...

    def list_unresolved(self) -> tuple[ModelDodVerifyRetryState, ...]:
        """Every recorded item currently in the terminal UNRESOLVED state."""
        ...


class FilesystemDodVerifyRetryLedger:
    """Filesystem implementation of :class:`ProtocolDodVerifyRetryLedger`."""

    def __init__(self, *, ticket_state_root: Path) -> None:
        self._root = Path(ticket_state_root).expanduser()

    def path_for(self, ticket_id: str) -> Path:
        """Where this ticket's retry state lives."""
        normalized = ticket_id.strip().upper()
        if not _TICKET_RE.match(normalized):
            raise ValueError(f"ticket_id must match OMN-<digits>; got {ticket_id!r}")
        return self._root / normalized / _RETRY_STATE_FILENAME

    def read(self, ticket_id: str) -> ModelDodVerifyRetryState | None:
        path = self.path_for(ticket_id)
        if not path.is_file():
            return None
        raw = path.read_text(encoding="utf-8")
        # A corrupt record must fail loudly. Treating it as "no record" would
        # silently restore the PENDING reading this ledger exists to prevent.
        return ModelDodVerifyRetryState.model_validate_json(raw)

    def write(self, state: ModelDodVerifyRetryState) -> None:
        existing = self.read(state.ticket_id)
        if existing is not None:
            _refuse_history_loss(existing=existing, incoming=state)
        path = self.path_for(state.ticket_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(state.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def list_unresolved(self) -> tuple[ModelDodVerifyRetryState, ...]:
        base = self._root
        if not base.is_dir():
            return ()
        found: list[ModelDodVerifyRetryState] = []
        for path in sorted(base.glob(f"*/{_RETRY_STATE_FILENAME}")):
            state = ModelDodVerifyRetryState.model_validate_json(
                path.read_text(encoding="utf-8")
            )
            if state.status is EnumDodVerifyStatus.UNRESOLVED:
                found.append(state)
        return tuple(found)


def _refuse_history_loss(
    *,
    existing: ModelDodVerifyRetryState,
    incoming: ModelDodVerifyRetryState,
) -> None:
    """Reject any write that is not a forward extension of what is recorded.

    Two failure shapes, both observed in practice as "the sweep ran again and
    the item looked untouched":

    * a shorter history — a fresh process built an empty state and is about to
      overwrite recorded attempts with a record that reads PENDING;
    * a rewritten prefix — the same attempt numbers with different outcomes,
      which would let a later green quietly replace a recorded unresolved.
    """
    if incoming.ticket_id.upper() != existing.ticket_id.upper():
        raise ValueError(
            f"ledger path collision: recorded {existing.ticket_id!r}, "
            f"incoming {incoming.ticket_id!r}"
        )
    if incoming.attempt_count < existing.attempt_count:
        raise ValueError(
            f"refusing to truncate {existing.ticket_id} retry history from "
            f"{existing.attempt_count} attempt(s) to {incoming.attempt_count}; "
            "an item that has been attempted may never read as never-attempted"
        )
    for index, recorded in enumerate(existing.attempts):
        if recorded.abandoned:
            # The one legal in-place edit: an abandoned attempt is reconciled
            # into a completed one by ``reconcile_abandoned_attempt``. Its
            # start is immutable; only the outcome is filled in.
            if incoming.attempts[index].started_at != recorded.started_at:
                raise ValueError(
                    f"refusing to rewrite the start of {existing.ticket_id} "
                    f"attempt {recorded.attempt_number}"
                )
            continue
        if incoming.attempts[index] != recorded:
            raise ValueError(
                f"refusing to rewrite recorded {existing.ticket_id} attempt "
                f"{recorded.attempt_number}; retry history is append-only"
            )


__all__: list[str] = [
    "FilesystemDodVerifyRetryLedger",
    "ProtocolDodVerifyRetryLedger",
]
