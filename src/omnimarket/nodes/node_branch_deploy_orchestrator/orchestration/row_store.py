# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Where branch deploy rows live, under compare-and-set (OMN-24058).

Two stores implement one protocol:

* :class:`InMemoryBranchDeployRowStore`: versioned rows in process, bounded by
  the overlay's ``context_limit``, for tests and local dispatch. A write whose
  expected version is not the stored version is refused, exactly like the
  durable table's ``version`` column.
* :class:`StateIoBranchDeployRowStore`: the runtime's ``state_io`` seam
  (omnibase_infra ``CONTEXTVAR_STATE_IO_ROWS``). The runtime loads the row
  before ``handle`` and persists what :class:`~..state_codec.StateIoCodec`
  flushes after it, under its own compare-and-set; a lost race re-runs the
  whole leg against the winning row. Here a write is staged for that flush.

There is no retry loop here: a leg that loses the race raises
:class:`BranchDeployRowConflictError` and the runtime re-runs it.
"""

from __future__ import annotations

import json
from typing import Protocol, runtime_checkable
from uuid import UUID

from omnimarket.nodes.node_branch_deploy_orchestrator.models.model_branch_deploy_orchestrator import (
    EnumBranchDeployPhase,
    ModelBranchDeployContext,
)

# Top-level keys omnibase_infra's state_io wiring reads without decoding the
# payload (tenant_id, state, in_flight), added at encode time. None of them is
# a row field (the context is extra="forbid"), so decode drops all three.
_STATE_KEY = "state"
_IN_FLIGHT_KEY = "in_flight"
_TENANT_KEY = "tenant_id"
_ENVELOPE_KEYS = (_STATE_KEY, _IN_FLIGHT_KEY, _TENANT_KEY)
# The runtime's give-up annotation (OMN-20119). omnibase_infra state_io
# ``recover_stale_rows`` writes it into the payload when it abandons a row whose
# effect never answered, and sets the ``in_flight`` column false. It is not a
# row field, so decode drops it.
_RUNTIME_RECOVERY_KEY = "failure_reason"
# A finished deploy's row reads COMPLETED to the runtime's row indexing; the
# phase itself is ``done``.
_COMPLETED_STATE = "COMPLETED"
# The branch deploy workflow is platform-internal: its rows belong to no tenant.
PLATFORM_TENANT = "platform"


class BranchDeployRowConflictError(RuntimeError):
    """A leg lost the compare-and-set race; the runtime re-runs it."""


@runtime_checkable
class ProtocolBranchDeployRowStore(Protocol):
    """Versioned rows keyed by the deploy's correlation id."""

    def load(self, key: str) -> tuple[ModelBranchDeployContext | None, int]:
        """The row stored under the key and its version, or no row at version 0."""

    def compare_and_set(
        self, key: str, expected_version: int, row: ModelBranchDeployContext
    ) -> bool:
        """Write the row when the stored version still matches the expected one."""


def encode_row(row: ModelBranchDeployContext) -> str:
    """Row JSON plus the three well-known top-level keys the runtime indexes."""
    payload = row.model_dump(mode="json")
    payload[_STATE_KEY] = (
        _COMPLETED_STATE if row.phase is EnumBranchDeployPhase.DONE else row.phase.value
    )
    payload[_IN_FLIGHT_KEY] = False
    payload[_TENANT_KEY] = PLATFORM_TENANT
    return json.dumps(payload, sort_keys=True)


def decode_row(raw: str | bytes) -> ModelBranchDeployContext:
    payload = json.loads(raw)
    if isinstance(payload, dict):
        for key in _ENVELOPE_KEYS:
            payload.pop(key, None)
        payload.pop(_RUNTIME_RECOVERY_KEY, None)
    return ModelBranchDeployContext.model_validate(payload)


class InMemoryBranchDeployRowStore:
    """Versioned in-process rows, at most ``limit`` of them. Version 0 means no row.

    ``limit`` is the overlay's ``context_limit``, which bounds this store only:
    over the limit the oldest finished (``done``) row is dropped first, else the
    oldest row.
    """

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._rows: dict[str, tuple[str, int]] = {}

    def load(self, key: str) -> tuple[ModelBranchDeployContext | None, int]:
        stored = self._rows.get(key)
        if stored is None:
            return None, 0
        raw, version = stored
        return decode_row(raw), version

    def compare_and_set(
        self, key: str, expected_version: int, row: ModelBranchDeployContext
    ) -> bool:
        current = self._rows.get(key)
        current_version = current[1] if current is not None else 0
        if current_version != expected_version:
            return False
        self._rows[key] = (encode_row(row), expected_version + 1)
        self._drop_over_limit()
        return True

    def active_keys(self) -> tuple[UUID, ...]:
        """The keys of the unfinished rows, in insertion order."""
        return tuple(
            UUID(key)
            for key, (raw, _version) in self._rows.items()
            if decode_row(raw).phase is not EnumBranchDeployPhase.DONE
        )

    def _drop_over_limit(self) -> None:
        """Drop the oldest finished row first, else the oldest row, until within the limit."""
        while len(self._rows) > self._limit:
            finished = [
                key
                for key, (raw, _version) in self._rows.items()
                if decode_row(raw).phase is EnumBranchDeployPhase.DONE
            ]
            del self._rows[finished[0] if finished else next(iter(self._rows))]


def read_active_state_io_rows() -> dict[str, tuple[str | None, int]] | None:
    """The runtime's loaded rows for the dispatch in progress, or None outside it."""
    try:
        from omnibase_infra.runtime.state_io.state_store_adapter import (
            CONTEXTVAR_STATE_IO_ROWS,
        )
    except ImportError:
        return None
    return CONTEXTVAR_STATE_IO_ROWS.get()


class StateIoBranchDeployRowStore:
    """The runtime ``state_io`` seam: load from the bound rows, stage writes for flush."""

    def __init__(self) -> None:
        self._staged: dict[str, str] = {}

    def load(self, key: str) -> tuple[ModelBranchDeployContext | None, int]:
        rows = read_active_state_io_rows()
        if rows is None:
            msg = "state_io rows are not bound for this dispatch"
            raise RuntimeError(msg)
        payload_json, version = rows.get(key, (None, 0))
        if payload_json is None:
            return None, version
        return decode_row(payload_json), version

    def compare_and_set(
        self, key: str, expected_version: int, row: ModelBranchDeployContext
    ) -> bool:
        # The runtime owns the real compare-and-set after handle() returns; a
        # lost race there re-runs this leg against the winning row.
        self._staged[key] = encode_row(row)
        return True

    def flush(self, key: str) -> str | None:
        return self._staged.pop(key, None)


__all__: list[str] = [
    "BranchDeployRowConflictError",
    "InMemoryBranchDeployRowStore",
    "ProtocolBranchDeployRowStore",
    "StateIoBranchDeployRowStore",
    "decode_row",
    "encode_row",
    "read_active_state_io_rows",
]
