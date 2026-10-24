# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed def-B handler: the work-state context source (OMN-21111).

``handle(ModelContextSourceRequest) -> ModelContextSourceResult`` answers two
factors from the work ledger at the request's revision (a UTC row stamp, the
ledger watermark), for the lanes the request names: the caller scope's
``lane`` and the id of its ``session:<id>`` scope.

* ``OPEN_WORK``: every ``msg:<id>`` entity the projection's fold leaves open (no
  ACK answers it) whose recipients include one of the lanes. With the
  contract's ``open_work_senders: self`` the sender must be one of the lanes
  too (the session's own handoff notes); ``any`` keeps every sender.
* ``ORCHESTRATION_STATE``: every ``claim:<lane>`` entity the fold leaves open (no
  TERMINAL or RELEASE closed it) whose lane, or whose opening CLAIM's
  ``parent=`` cell, is one of the lanes.

An entity opened longer than the contract's ``horizon_hours`` before the
watermark is stale and left out (counted in ``entry_counts``). Each entry's
text is the opening row whole. A tenant the binding does not list is
``denied``; a revision that is not a row stamp, or a request naming no lane, is
an ``invalid_request`` error; a failed read is ``unavailable``.
"""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Iterable
from datetime import datetime, timedelta
from pathlib import Path
from typing import ClassVar, Literal

import yaml
from omnibase_core.enums.enum_context_factor import EnumContextFactor
from omnibase_core.enums.enum_context_source_error_code import (
    EnumContextSourceErrorCode,
)
from omnibase_core.enums.enum_context_source_status import EnumContextSourceStatus
from omnibase_core.enums.enum_context_span_unit import EnumContextSpanUnit
from omnibase_core.models.context.model_context_source_entry import (
    ModelContextSourceEntry,
)
from omnibase_core.models.context.model_context_source_request import (
    ModelContextSourceRequest,
)
from omnibase_core.models.context.model_context_source_result import (
    ModelContextSourceResult,
)
from omnibase_core.models.context.model_context_source_span import (
    ModelContextSourceSpan,
)
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.enums.enum_work_ledger_entity_kind import EnumWorkLedgerEntityKind
from omnimarket.handlers.work_ledger_fold import (
    SHARED_WORK_LEDGER_FOLD_MEMO,
    WorkLedgerFoldMemo,
    apply_ops,
    close_relationships,
    parse_stamp,
)
from omnimarket.models.model_work_ledger_fold_result import ModelWorkLedgerLink
from omnimarket.nodes.contract_topics import contract_publish_topics
from omnimarket.nodes.node_context_source_ledger_effect.handlers.ledger_row_reader import (
    LedgerSourceReadError,
    ModelLedgerSourceRow,
    PostgresLedgerRowReader,
    ProtocolLedgerRowReader,
    load_ledger_source_binding,
)

_CONTRACT_PATH = Path(__file__).parent.parent / "contract.yaml"
_SOURCE_NAME = "work_ledger"
_SESSION_SCOPE = "session:"
_STAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
SERVED_FACTORS = frozenset(
    {EnumContextFactor.OPEN_WORK, EnumContextFactor.ORCHESTRATION_STATE}
)
_KIND_OF = {
    EnumContextFactor.OPEN_WORK: EnumWorkLedgerEntityKind.MSG,
    EnumContextFactor.ORCHESTRATION_STATE: EnumWorkLedgerEntityKind.CLAIM,
}


class ModelWorkStateBinding(BaseModel):
    """The contract's ``context_source_work_state`` block."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    horizon_hours: int = Field(gt=0)
    open_work_senders: Literal["self", "any"]


def load_work_state_binding(contract_path: Path) -> ModelWorkStateBinding:
    """Load and validate the contract's ``context_source_work_state`` block."""
    raw = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    block = raw.get("context_source_work_state") if isinstance(raw, dict) else None
    if not isinstance(block, dict):
        raise ValueError("context_source_work_state must be a mapping")
    return ModelWorkStateBinding.model_validate(block)


def cell_value(raw_row: str, key: str) -> str | None:
    """The value of the row's first ``key=`` cell, or None."""
    prefix = f"{key}="
    for cell in (part.strip() for part in raw_row.split("|")[2:]):
        if cell.startswith(prefix):
            return cell[len(prefix) :]
    return None


def requested_lanes(request: ModelContextSourceRequest) -> frozenset[str]:
    """The caller scope's lane and the ``session:<id>`` scope's id."""
    lanes = {
        scope.removeprefix(_SESSION_SCOPE)
        for scope in request.scopes
        if scope.startswith(_SESSION_SCOPE) and scope != _SESSION_SCOPE
    }
    if request.caller_scope.lane:
        lanes.add(request.caller_scope.lane)
    return frozenset(lanes)


class HandlerContextSourceWorkState:
    """The work-state tier behind ``ProtocolContextSource``."""

    synchronous_execution: ClassVar[bool] = True

    def __init__(
        self,
        *,
        contract_path: Path | None = None,
        reader: ProtocolLedgerRowReader | None = None,
        tenant_ids: Iterable[str] | None = None,
        work_state: ModelWorkStateBinding | None = None,
        fold_memo: WorkLedgerFoldMemo | None = None,
    ) -> None:
        self._contract_path = contract_path or _CONTRACT_PATH
        self.terminal_event = contract_publish_topics(self._contract_path)[0]
        binding = load_ledger_source_binding(self._contract_path)
        self._tenant_ids = frozenset(
            tenant_ids if tenant_ids is not None else binding.tenant_ids
        )
        self._work_state = work_state or load_work_state_binding(self._contract_path)
        self._horizon = timedelta(hours=self._work_state.horizon_hours)
        self._reader = (
            reader if reader is not None else PostgresLedgerRowReader(binding)
        )
        self._fold_memo = (
            fold_memo if fold_memo is not None else SHARED_WORK_LEDGER_FOLD_MEMO
        )

    def handle(self, request: ModelContextSourceRequest) -> ModelContextSourceResult:
        if not isinstance(request, ModelContextSourceRequest):
            raise TypeError("work-state context source requires its contract payload")
        if request.access.tenant_id not in self._tenant_ids:
            return ModelContextSourceResult(status=EnumContextSourceStatus.DENIED)
        if request.factor not in SERVED_FACTORS:
            return ModelContextSourceResult(status=EnumContextSourceStatus.UNRESOLVED)
        if not _STAMP.match(request.revision):
            return ModelContextSourceResult.error(
                EnumContextSourceErrorCode.INVALID_REQUEST,
                f"revision {request.revision!r} is not a ledger row stamp"
                " (YYYY-MM-DDTHH:MM:SSZ)",
            )
        lanes = requested_lanes(request)
        if not lanes:
            return ModelContextSourceResult.error(
                EnumContextSourceErrorCode.INVALID_REQUEST,
                "work state needs a caller-scope lane or a `session:<id>` scope",
            )
        as_of = parse_stamp(request.revision)

        started = time.monotonic()
        try:
            rows = self._reader.read_rows(as_of=as_of)
        except LedgerSourceReadError:
            return ModelContextSourceResult(status=EnumContextSourceStatus.UNAVAILABLE)
        read_ms = int((time.monotonic() - started) * 1000)

        started = time.monotonic()
        entries, stale = self._entries(rows, as_of, request.factor, lanes)
        fold_ms = int((time.monotonic() - started) * 1000)
        timings = {"read": read_ms, "fold": fold_ms}
        counts = {_SOURCE_NAME: len(entries), "stale": stale}
        if not entries:
            return ModelContextSourceResult(
                status=EnumContextSourceStatus.UNRESOLVED,
                entry_counts=counts,
                leg_timings_ms=timings,
            )
        return ModelContextSourceResult(
            status=EnumContextSourceStatus.RESOLVED,
            entries=entries,
            entry_counts=counts,
            leg_timings_ms=timings,
        )

    def _entries(
        self,
        rows: tuple[ModelLedgerSourceRow, ...],
        as_of: datetime,
        factor: EnumContextFactor,
        lanes: frozenset[str],
    ) -> tuple[tuple[ModelContextSourceEntry, ...], int]:
        by_id: dict[str, ModelLedgerSourceRow] = {}
        state: dict[str, dict[str, object]] = {}
        links: list[ModelWorkLedgerLink] = []
        results = self._fold_memo.fold_rows([(row.row_id, row.raw_row) for row in rows])
        for row, result in zip(rows, results, strict=True):
            if result is None:
                continue
            # The fold's own row id is what its entities name as opened_row_id.
            by_id.setdefault(result.row.row_id, row)
            apply_ops(state, result.ops)
            links.extend(result.links)
        closure, _unresolved = close_relationships(state, links, as_of=as_of)

        kind = _KIND_OF[factor].value
        oldest = as_of - self._horizon
        stale = 0
        picked: list[tuple[datetime, str, ModelLedgerSourceRow]] = []
        for key, entity in closure.items():
            if entity["kind"] != kind or not entity["is_open"]:
                continue
            row_id, opened = entity["opened_row_id"], entity["opened_at"]
            if not isinstance(row_id, str) or not isinstance(opened, datetime):
                continue
            opener = by_id.get(row_id)
            if opener is None or not self._names(
                opener, entity, factor, lanes, self._work_state
            ):
                continue
            if opened < oldest:
                stale += 1
                continue
            picked.append((opened, key, opener))
        picked.sort(key=lambda item: (item[0], item[1]))
        return tuple(_entry(factor, row, as_of) for _, _, row in picked), stale

    @staticmethod
    def _names(
        row: ModelLedgerSourceRow,
        entity: dict[str, object],
        factor: EnumContextFactor,
        lanes: frozenset[str],
        work_state: ModelWorkStateBinding,
    ) -> bool:
        if factor is EnumContextFactor.OPEN_WORK:
            recipients = entity["scope_to"]
            if work_state.open_work_senders == "self" and entity["lane"] not in lanes:
                return False
            return isinstance(recipients, str) and bool(
                lanes.intersection(recipients.split(","))
            )
        return entity["lane"] in lanes or cell_value(row.raw_row, "parent") in lanes


def _entry(
    factor: EnumContextFactor, row: ModelLedgerSourceRow, as_of: datetime
) -> ModelContextSourceEntry:
    text = row.raw_row
    return ModelContextSourceEntry(
        factor=factor,
        text=text,
        source_ref=row.source_ref,
        span=ModelContextSourceSpan(
            unit=EnumContextSpanUnit.CHARACTER, start=0, end=len(text)
        ),
        text_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        status=EnumContextSourceStatus.RESOLVED,
        verbatim_verified=True,
        as_of=as_of,
    )


__all__ = [
    "SERVED_FACTORS",
    "HandlerContextSourceWorkState",
    "ModelWorkStateBinding",
    "cell_value",
    "load_work_state_binding",
    "requested_lanes",
]
