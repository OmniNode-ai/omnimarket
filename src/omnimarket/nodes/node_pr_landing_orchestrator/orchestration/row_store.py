# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Where landing rows live, under compare-and-set (plan 5.1 revision 1, F8).

Two stores implement one protocol:

* :class:`InMemoryPrLandingRowStore`: versioned rows in process, for tests and
  local dispatch. A write whose expected version is not the stored version is
  refused, exactly like the durable table's ``version`` column.
* :class:`StateIoPrLandingRowStore`: the runtime's ``state_io`` seam
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

from omnimarket.nodes.node_pr_landing_orchestrator.models.model_pr_landing_workflow_row import (
    ModelPrLandingWorkflowRow,
)

# Top-level keys omnibase_infra's state_io wiring reads without decoding the
# payload (tenant_id, state, in_flight), added at encode time.
_STATE_KEY = "state"
_IN_FLIGHT_KEY = "in_flight"
_TENANT_KEY = "tenant_id"
_ENVELOPE_KEYS = (_STATE_KEY, _IN_FLIGHT_KEY, _TENANT_KEY)
# The runtime's give-up annotation (OMN-20119). omnibase_infra state_io
# ``recover_stale_rows`` writes it into the payload when it abandons a row whose
# effect never answered, and sets the ``in_flight`` column false. It is not a
# row field: an undecodable row failed every later message for that PR, which
# the dead-letter replay then put back on the shared command topic.
_RUNTIME_RECOVERY_KEY = "failure_reason"
_NO_ROW_STATE = "UNSEEN"
# The landing workflow is platform-internal: its rows belong to no tenant.
PLATFORM_TENANT = "platform"

DEFAULT_CAS_ATTEMPTS = 5


class PrLandingCasExhaustedError(RuntimeError):
    """Every attempt lost the compare-and-set race."""


@runtime_checkable
class ProtocolPrLandingRowStore(Protocol):
    """Versioned rows keyed by ``landing_key``."""

    async def load(self, key: str) -> tuple[ModelPrLandingWorkflowRow | None, int]: ...

    async def compare_and_set(
        self, key: str, expected_version: int, row: ModelPrLandingWorkflowRow
    ) -> bool: ...


def encode_row(row: ModelPrLandingWorkflowRow) -> str:
    """Row JSON plus the three well-known top-level keys the runtime indexes."""
    payload = row.model_dump(mode="json")
    landing = row.landing
    payload[_STATE_KEY] = landing.state.value if landing is not None else _NO_ROW_STATE
    payload[_IN_FLIGHT_KEY] = row.effect_in_flight is not None
    payload[_TENANT_KEY] = PLATFORM_TENANT
    return json.dumps(payload, sort_keys=True)


def decode_row(raw: str | bytes) -> ModelPrLandingWorkflowRow:
    payload = json.loads(raw)
    if isinstance(payload, dict):
        for key in _ENVELOPE_KEYS:
            payload.pop(key, None)
        if payload.pop(_RUNTIME_RECOVERY_KEY, None) is not None:
            # The runtime gave the in-flight effect up; its answer is never
            # coming, so R4 must not keep the PR waiting on it.
            payload["effect_in_flight"] = None
    return ModelPrLandingWorkflowRow.model_validate(payload)


class InMemoryPrLandingRowStore:
    """Versioned in-process rows. Version 0 means no row."""

    def __init__(self) -> None:
        self._rows: dict[str, tuple[str, int]] = {}

    async def load(self, key: str) -> tuple[ModelPrLandingWorkflowRow | None, int]:
        stored = self._rows.get(key)
        if stored is None:
            return None, 0
        raw, version = stored
        return decode_row(raw), version

    async def compare_and_set(
        self, key: str, expected_version: int, row: ModelPrLandingWorkflowRow
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


class StateIoPrLandingRowStore:
    """The runtime ``state_io`` seam: load from the bound rows, stage writes for flush."""

    def __init__(self) -> None:
        self._staged: dict[str, str] = {}

    async def load(self, key: str) -> tuple[ModelPrLandingWorkflowRow | None, int]:
        rows = read_active_state_io_rows()
        if rows is None:
            msg = "state_io rows are not bound for this dispatch"
            raise RuntimeError(msg)
        payload_json, version = rows.get(key, (None, 0))
        if payload_json is None:
            return None, version
        return decode_row(payload_json), version

    async def compare_and_set(
        self, key: str, expected_version: int, row: ModelPrLandingWorkflowRow
    ) -> bool:
        # The runtime owns the real compare-and-set after handle() returns; a
        # lost race there re-runs this leg against the winning row.
        self._staged[key] = encode_row(row)
        return True

    def flush(self, key: str) -> str | None:
        return self._staged.pop(key, None)


async def run_with_cas_retry[T](
    store: ProtocolPrLandingRowStore,
    key: str,
    decide: Callable[
        [ModelPrLandingWorkflowRow | None],
        Awaitable[tuple[ModelPrLandingWorkflowRow | None, T]],
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
    raise PrLandingCasExhaustedError(msg)


__all__: list[str] = [
    "DEFAULT_CAS_ATTEMPTS",
    "PLATFORM_TENANT",
    "InMemoryPrLandingRowStore",
    "PrLandingCasExhaustedError",
    "ProtocolPrLandingRowStore",
    "StateIoPrLandingRowStore",
    "decode_row",
    "encode_row",
    "read_active_state_io_rows",
    "run_with_cas_retry",
]
