# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed def-B handler: the work-ledger context source (OMN-21127).

``handle(ModelContextSourceRequest) -> ModelLedgerSourceResult`` answers the
``OPERATOR_RULINGS`` factor. It reads every row up to the request's revision
(a UTC row stamp, the ledger watermark), folds them through the projection's
fold and ``close_relationships`` (the relationship rules TA.5 added), and
returns one entry per RULING or OPERATOR-CONSENT row: the cited stamps when the
caller scope names any, otherwise every row still in force. Each entry's text
is the row's ``raw_row`` whole, and every CORRECTION citing the row rides in
``corrected_by``, so a ruling never leaves this source without its corrections.

A request for a tenant the binding does not list is ``denied``; a revision that
is not a row stamp is an ``invalid_request`` error; a failed read is
``unavailable``. None of them is reported as an empty ``resolved``.
"""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import ClassVar

from omnibase_core.enums.enum_context_factor import EnumContextFactor
from omnibase_core.enums.enum_context_source_error_code import (
    EnumContextSourceErrorCode,
)
from omnibase_core.enums.enum_context_source_status import EnumContextSourceStatus
from omnibase_core.enums.enum_context_span_unit import EnumContextSpanUnit
from omnibase_core.enums.enum_context_supersession_status import (
    EnumContextSupersessionStatus,
)
from omnibase_core.models.context.model_context_source_request import (
    ModelContextSourceRequest,
)
from omnibase_core.models.context.model_context_source_span import (
    ModelContextSourceSpan,
)

from omnimarket.nodes.contract_topics import contract_publish_topics
from omnimarket.nodes.node_context_source_ledger_effect.handlers.ledger_row_reader import (
    LedgerSourceReadError,
    ModelLedgerSourceRow,
    PostgresLedgerRowReader,
    ProtocolLedgerRowReader,
    load_ledger_source_binding,
)
from omnimarket.nodes.node_context_source_ledger_effect.models.model_ledger_source_entry import (
    CORRECTED_LABEL,
    FRAMING_LABEL,
    ModelLedgerCorrection,
    ModelLedgerSourceEntry,
    ModelLedgerSourceResult,
)
from omnimarket.nodes.node_projection_work_ledger.handlers.work_ledger_fold import (
    WorkLedgerFoldError,
    apply_ops,
    close_relationships,
    fold_row,
    parse_stamp,
)
from omnimarket.nodes.node_projection_work_ledger.models.enum_work_ledger_entity_kind import (
    EnumWorkLedgerEntityKind,
    EnumWorkLedgerStateOp,
)
from omnimarket.nodes.node_projection_work_ledger.models.model_work_ledger_fold_request import (
    ModelWorkLedgerFoldRequest,
)
from omnimarket.nodes.node_projection_work_ledger.models.model_work_ledger_fold_result import (
    ModelWorkLedgerFoldResult,
    ModelWorkLedgerLink,
)

_CONTRACT_PATH = Path(__file__).parent.parent / "contract.yaml"
_SOURCE_NAME = "work_ledger"
_STAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_SPAN = re.compile(r"^(?P<start>\d+):(?P<end>\d+)$")
# The grammar's quoted-words pattern: the first cell (other than question=)
# holding a straight- or curly-quoted run is the row's quoted cell.
_QUOTED = re.compile(r'"[^"\n]*\S[^"\n]*"|“[^”\n]*\S[^”\n]*”')
_QUESTION = re.compile(r"(?i)^question=")
_AUTHORITY_KINDS = frozenset(
    {EnumWorkLedgerEntityKind.RULING, EnumWorkLedgerEntityKind.CONSENT}
)


def quoted_words(raw_row: str) -> str | None:
    """The text inside the row's first quoted cell, without its quotes."""
    for cell in (part.strip() for part in raw_row.split("|")[2:]):
        if _QUESTION.match(cell):
            continue
        match = _QUOTED.search(cell)
        if match:
            return match.group(0)[1:-1]
    return None


def _span_bounds(value: str) -> tuple[int, int] | None:
    match = _SPAN.match(value)
    if match is None:
        return None
    start, end = int(match.group("start")), int(match.group("end"))
    return (start, end) if start < end else None


class HandlerContextSourceLedger:
    """The ledger tier behind ``ProtocolContextSource``."""

    synchronous_execution: ClassVar[bool] = True

    def __init__(
        self,
        *,
        contract_path: Path | None = None,
        reader: ProtocolLedgerRowReader | None = None,
        tenant_ids: Iterable[str] | None = None,
    ) -> None:
        self._contract_path = contract_path or _CONTRACT_PATH
        self.terminal_event = contract_publish_topics(self._contract_path)[0]
        binding = load_ledger_source_binding(self._contract_path)
        self._tenant_ids = frozenset(
            tenant_ids if tenant_ids is not None else binding.tenant_ids
        )
        self._reader = (
            reader if reader is not None else PostgresLedgerRowReader(binding)
        )

    def handle(self, request: ModelContextSourceRequest) -> ModelLedgerSourceResult:
        if not isinstance(request, ModelContextSourceRequest):
            raise TypeError("ledger context source requires its contract payload")
        if request.access.tenant_id not in self._tenant_ids:
            return ModelLedgerSourceResult(status=EnumContextSourceStatus.DENIED)
        if not _STAMP.match(request.revision):
            return ModelLedgerSourceResult.error(
                EnumContextSourceErrorCode.INVALID_REQUEST,
                f"revision {request.revision!r} is not a ledger row stamp"
                " (YYYY-MM-DDTHH:MM:SSZ)",
            )
        if request.factor is not EnumContextFactor.OPERATOR_RULINGS:
            return ModelLedgerSourceResult(status=EnumContextSourceStatus.UNRESOLVED)
        as_of = parse_stamp(request.revision)

        started = time.monotonic()
        try:
            rows = self._reader.read_rows(as_of=as_of)
        except LedgerSourceReadError:
            return ModelLedgerSourceResult(status=EnumContextSourceStatus.UNAVAILABLE)
        read_ms = int((time.monotonic() - started) * 1000)

        started = time.monotonic()
        entries = self._entries(rows, as_of, request.caller_scope.cited_ruling_stamps)
        fold_ms = int((time.monotonic() - started) * 1000)
        timings = {"read": read_ms, "fold": fold_ms}
        if not entries:
            return ModelLedgerSourceResult(
                status=EnumContextSourceStatus.UNRESOLVED, leg_timings_ms=timings
            )
        return ModelLedgerSourceResult(
            status=EnumContextSourceStatus.RESOLVED,
            entries=entries,
            entry_counts={_SOURCE_NAME: len(entries)},
            leg_timings_ms=timings,
        )

    def _entries(
        self,
        rows: tuple[ModelLedgerSourceRow, ...],
        as_of: datetime,
        cited: tuple[str, ...],
    ) -> tuple[ModelLedgerSourceEntry, ...]:
        by_id: dict[str, ModelLedgerSourceRow] = {}
        folded: dict[str, ModelWorkLedgerFoldResult] = {}
        state: dict[str, dict[str, object]] = {}
        links: list[ModelWorkLedgerLink] = []
        for row in rows:
            try:
                result = fold_row(ModelWorkLedgerFoldRequest(raw_row=row.raw_row))
            except WorkLedgerFoldError:
                continue
            by_id.setdefault(row.row_id, row)
            folded.setdefault(row.row_id, result)
            apply_ops(state, result.ops)
            links.extend(result.links)
        closure, _unresolved = close_relationships(state, links, as_of=as_of)

        openers: dict[str, list[str]] = {}
        for row_id, result in folded.items():
            for op in result.ops:
                if op.kind in _AUTHORITY_KINDS and op.op is EnumWorkLedgerStateOp.OPEN:
                    openers.setdefault(op.entity_key, []).append(row_id)

        wanted = set(cited)
        entries: list[ModelLedgerSourceEntry] = []
        for key, row_ids in openers.items():
            entity = closure[key]
            for row_id in row_ids:
                stamp = folded[row_id].row.row_ts.strftime("%Y-%m-%dT%H:%M:%SZ")
                if wanted and stamp not in wanted:
                    continue
                if not wanted and not entity["is_open"]:
                    continue
                entries.append(
                    self._entry(
                        by_id[row_id],
                        folded[row_id],
                        stamp,
                        entity,
                        folded,
                        by_id,
                        as_of,
                        ambiguous=len(row_ids) > 1,
                    )
                )
        entries.sort(key=lambda entry: (entry.row_stamp, entry.row_id))
        return tuple(entries)

    @staticmethod
    def _entry(
        row: ModelLedgerSourceRow,
        result: ModelWorkLedgerFoldResult,
        stamp: str,
        entity: dict[str, object],
        folded: dict[str, ModelWorkLedgerFoldResult],
        by_id: dict[str, ModelLedgerSourceRow],
        as_of: datetime,
        *,
        ambiguous: bool,
    ) -> ModelLedgerSourceEntry:
        spans = dict(_pairs(entity["operator_spans"]))
        corrections = tuple(
            ModelLedgerCorrection(
                row_id=cid,
                row_stamp=folded[cid].row.row_ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
                text=by_id[cid].raw_row,
                operator_span=spans.get(cid),
            )
            for cid in _ids(entity["corrected_by"])
        )
        if _ids(entity["superseded_by"]):
            supersession = EnumContextSupersessionStatus.SUPERSEDED
        elif corrections:
            supersession = EnumContextSupersessionStatus.CORRECTED
        elif _ids(entity["amended_by"]) or _ids(entity["extended_by"]):
            supersession = EnumContextSupersessionStatus.AMENDED
        else:
            supersession = EnumContextSupersessionStatus.ACTIVE

        operator_words: str | None = None
        framing: tuple[str, str] | None = None
        label: str | None = CORRECTED_LABEL if corrections else None
        typed = sorted({span for span in spans.values() if span})
        if len(typed) > 1:
            ambiguous = True
        elif typed:
            bounds = _span_bounds(typed[0])
            quote = quoted_words(row.raw_row)
            if bounds is None or quote is None or bounds[1] > len(quote):
                ambiguous = True
            else:
                start, end = bounds
                operator_words = quote[start:end]
                framing = (quote[:start], quote[end:])
                label = FRAMING_LABEL

        text = row.raw_row
        return ModelLedgerSourceEntry(
            factor=EnumContextFactor.OPERATOR_RULINGS,
            text=text,
            source_ref=row.source_ref,
            span=ModelContextSourceSpan(
                unit=EnumContextSpanUnit.CHARACTER, start=0, end=len(text)
            ),
            text_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            status=EnumContextSourceStatus.AMBIGUOUS
            if ambiguous
            else EnumContextSourceStatus.RESOLVED,
            supersession_status=supersession,
            verbatim_verified=True,
            as_of=as_of,
            row_id=row.row_id,
            row_type=result.row.row_type,
            row_stamp=stamp,
            corrected_by=corrections,
            operator_words=operator_words,
            recorded_framing=framing,
            framing_label=label,
        )


def _ids(value: object) -> tuple[str, ...]:
    return (
        tuple(v for v in value if isinstance(v, str))
        if isinstance(value, tuple)
        else ()
    )


def _pairs(value: object) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, tuple):
        return ()
    return tuple(
        (pair[0], pair[1])
        for pair in value
        if isinstance(pair, tuple)
        and len(pair) == 2
        and isinstance(pair[0], str)
        and isinstance(pair[1], str)
    )


__all__ = ["HandlerContextSourceLedger", "quoted_words"]
