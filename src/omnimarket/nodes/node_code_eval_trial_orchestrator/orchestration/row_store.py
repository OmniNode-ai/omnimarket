# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Where code eval rows live, under compare-and-set (OMN-23627).

Two stores implement one protocol:

* :class:`InMemoryCodeEvalRowStore`: versioned rows in process, for tests and
  local dispatch. A write whose expected version is not the stored version is
  refused, exactly like the durable table's ``version`` column.
* :class:`StateIoCodeEvalRowStore`: the runtime's ``state_io`` seam
  (omnibase_infra ``CONTEXTVAR_STATE_IO_ROWS``). The runtime loads the row
  before ``handle`` and persists what :class:`~..state_codec.StateIoCodec`
  flushes after it, under its own compare-and-set; a lost race re-runs the
  whole leg against the winning row. Here a write is staged for that flush.

:func:`run_with_cas_retry` is the leg loop: load, decide, write under the
version it loaded, and on a lost race reload and decide again.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Protocol, runtime_checkable

from omnimarket.models.code_eval_trial.model_code_eval_run_row import (
    ModelCodeEvalRunRow,
)

# Top-level keys omnibase_infra's state_io wiring reads without decoding the
# payload (tenant_id, state, in_flight), added at encode time. ``state`` and
# ``in_flight`` are also the row's own fields, with the same values, so decode keeps
# them; only ``tenant_id``, which the row holds on its request, is dropped.
_STATE_KEY = "state"
_IN_FLIGHT_KEY = "in_flight"
_TENANT_KEY = "tenant_id"
_ENVELOPE_KEYS = (_TENANT_KEY,)
# The runtime's give-up annotation (OMN-20119). omnibase_infra state_io
# ``recover_stale_rows`` writes it into the payload when it abandons a row whose
# effect never answered, and sets the ``in_flight`` column false. It is not a
# row field: an undecodable row failed every later message for that run, which
# the dead-letter replay then put back on the shared command topic.
_RUNTIME_RECOVERY_KEY = "failure_reason"

DEFAULT_CAS_ATTEMPTS = 5


class CodeEvalCasExhaustedError(RuntimeError):
    """Every attempt lost the compare-and-set race."""


@runtime_checkable
class ProtocolCodeEvalRowStore(Protocol):
    """Versioned rows keyed by the run id (``str(row.run_id)``)."""

    async def load(self, key: str) -> tuple[ModelCodeEvalRunRow | None, int]:
        """The row stored under the key and its version, or no row at version 0."""

    async def compare_and_set(
        self, key: str, expected_version: int, row: ModelCodeEvalRunRow
    ) -> bool:
        """Write the row when the stored version still matches the expected one."""


def encode_row(row: ModelCodeEvalRunRow) -> str:
    """Row JSON plus the three well-known top-level keys the runtime indexes."""
    payload = row.model_dump(mode="json")
    payload[_STATE_KEY] = row.state
    payload[_IN_FLIGHT_KEY] = row.in_flight
    payload[_TENANT_KEY] = row.request.tenant_id
    return json.dumps(payload, sort_keys=True)


def decode_row(raw: str | bytes) -> ModelCodeEvalRunRow:
    payload = json.loads(raw)
    if isinstance(payload, dict):
        for key in _ENVELOPE_KEYS:
            payload.pop(key, None)
        if payload.pop(_RUNTIME_RECOVERY_KEY, None) is not None:
            # The runtime gave the in-flight effect up; its answer is never
            # coming, so the reducer must not keep the run waiting on it.
            payload["in_flight"] = False
    return ModelCodeEvalRunRow.model_validate(payload)


class InMemoryCodeEvalRowStore:
    """Versioned in-process rows. Version 0 means no row."""

    def __init__(self) -> None:
        self._rows: dict[str, tuple[str, int]] = {}

    async def load(self, key: str) -> tuple[ModelCodeEvalRunRow | None, int]:
        stored = self._rows.get(key)
        if stored is None:
            return None, 0
        raw, version = stored
        return decode_row(raw), version

    async def compare_and_set(
        self, key: str, expected_version: int, row: ModelCodeEvalRunRow
    ) -> bool:
        current = self._rows.get(key)
        current_version = current[1] if current is not None else 0
        if current_version != expected_version:
            return False
        self._rows[key] = (encode_row(row), expected_version + 1)
        return True

    def version(self, key: str) -> int:
        stored = self._rows.get(key)
        return stored[1] if stored is not None else 0


def read_active_state_io_rows() -> dict[str, tuple[str | None, int]] | None:
    """The runtime's loaded rows for the dispatch in progress, or None outside it."""
    try:
        from omnibase_infra.runtime.state_io.state_store_adapter import (
            CONTEXTVAR_STATE_IO_ROWS,
        )
    except ImportError:
        return None
    return CONTEXTVAR_STATE_IO_ROWS.get()


class StateIoCodeEvalRowStore:
    """The runtime ``state_io`` seam: load from the bound rows, stage writes for flush."""

    def __init__(self) -> None:
        self._staged: dict[str, str] = {}

    async def load(self, key: str) -> tuple[ModelCodeEvalRunRow | None, int]:
        rows = read_active_state_io_rows()
        if rows is None:
            msg = "state_io rows are not bound for this dispatch"
            raise RuntimeError(msg)
        payload_json, version = rows.get(key, (None, 0))
        if payload_json is None:
            return None, version
        return decode_row(payload_json), version

    async def compare_and_set(
        self, key: str, expected_version: int, row: ModelCodeEvalRunRow
    ) -> bool:
        # The runtime owns the real compare-and-set after handle() returns; a
        # lost race there re-runs this leg against the winning row.
        self._staged[key] = encode_row(row)
        return True

    def flush(self, key: str) -> str | None:
        return self._staged.pop(key, None)


async def run_with_cas_retry[T](
    store: ProtocolCodeEvalRowStore,
    key: str,
    decide: Callable[
        [ModelCodeEvalRunRow | None],
        Awaitable[tuple[ModelCodeEvalRunRow | None, T]],
    ],
    *,
    attempts: int = DEFAULT_CAS_ATTEMPTS,
) -> T:
    """Load, decide, write under the loaded version; retry on a lost race.

    ``decide`` returns the row to write (None: nothing to write) and the
    leg's result. It is re-run from a fresh load on every retry, so a result
    from a losing attempt is never returned.
    """
    for _attempt in range(attempts):
        row, version = await store.load(key)
        new_row, result = await decide(row)
        if new_row is None:
            return result
        if await store.compare_and_set(key, version, new_row):
            return result
    msg = f"compare-and-set on {key} lost {attempts} times in a row"
    raise CodeEvalCasExhaustedError(msg)


__all__: list[str] = [
    "DEFAULT_CAS_ATTEMPTS",
    "CodeEvalCasExhaustedError",
    "InMemoryCodeEvalRowStore",
    "ProtocolCodeEvalRowStore",
    "StateIoCodeEvalRowStore",
    "decode_row",
    "encode_row",
    "read_active_state_io_rows",
    "run_with_cas_retry",
]
