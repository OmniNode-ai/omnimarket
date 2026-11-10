# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Compaction ground truth: the must-survive set at a boundary, derived from the ledger.

Pure and deterministic: the caller passes the pinned ledger prefix as raw rows, the
boundary instant ``now`` and the session scope; nothing is read from disk or a clock.
The set has eight kinds (plan section 3.3). Open, superseded, expired and corrected
come from the shared work-ledger fold (the TA.5 reducer), as of ``now``; this module
adds the session's scope and the kinds the fold does not model (asks answered by
``qkey``/``re=``, sent promises, unsent drafts).

Scope. The lane family is the orchestrator lane, ``extra_lanes`` and every lane whose
CLAIM or MSG row names an orchestrator lane in ``parent=``. State kinds (OPEN_CLAIM,
HOLD, OPEN_ASK, UNACKED_MSG) are read as of ``now`` for the family. Record kinds
(RULING, OPERATOR_CONSENT, PROMISE_SENT, DRAFT_UNSENT) must also be stamped inside
``[window_start, now]``; a ruling or consent composes with the in-force rulings that
amend or extend it (and those it amends), even when they predate the window.

Row conventions the ledger does not enforce, fixed here so a derivation never guesses:
a draft is a row of a family lane with a ``draft=<path>`` cell (and optionally
``thread=<id>``); it is sent by a later row that carries SENT-ON-BEHALF or a ``sent=``
cell and names the draft path or thread. A promise is a MSG of a family lane that carries
SENT-ON-BEHALF, or a family-lane row with a ``promise=`` cell. The ledger records no
spend of an OPERATOR-CONSENT, so the request names the spent ones.

Limit: the reducer keeps one open claim per lane, so an older unclosed claim of a lane
that has since claimed again is not a separate item.
"""

from __future__ import annotations

import hashlib
import json

from omnimarket.events.model_ledger_row_event import (
    ModelLedgerClaimEvent,
    ModelLedgerMsgEvent,
    ModelLedgerRowEventBase,
    ModelLedgerTerminalEvent,
)
from omnimarket.handlers.work_ledger_fold import parse_stamp
from omnimarket.nodes.node_compaction_ground_truth_compute.handlers.ledger_view import (
    LedgerView,
    build_view,
    cell,
    split_list,
)
from omnimarket.nodes.node_compaction_ground_truth_compute.handlers.required_spans import (
    content_spans,
)
from omnimarket.nodes.node_compaction_ground_truth_compute.models import (
    EXPECTED_CONDUCT,
    EnumGroundTruthKind,
    ModelGroundTruthItem,
    ModelGroundTruthRequest,
    ModelGroundTruthResult,
    ModelGroundTruthRow,
    ModelKindCount,
)

__all__ = ["HandlerCompactionGroundTruth"]

K = EnumGroundTruthKind
_SENT_ON_BEHALF = "SENT-ON-BEHALF"
_KIND_ORDER = {kind: index for index, kind in enumerate(K)}


def _quote(event: ModelLedgerRowEventBase) -> str:
    """The quoted cell of a row when it has one, else its free text, else its raw row."""
    for part in event.free_text.split(" | "):
        part = part.strip()
        if len(part) >= 2 and part[0] == part[-1] == '"':
            return part[1:-1]
    return event.free_text or event.raw_row


def _row(
    event: ModelLedgerRowEventBase, role: str, operator_span: str | None = None
) -> ModelGroundTruthRow:
    return ModelGroundTruthRow(
        stamp=event.row_timestamp,
        row_type=event.row_type.value,
        lane=event.row_lane,
        row_id=event.row_id,
        role=role,
        quote=_quote(event),
        operator_span=operator_span,
    )


def _ids(entity: dict[str, object], key: str) -> tuple[str, ...]:
    """Row ids the reducer lists under ``key`` (``amended_by``, ``corrected_by``, ...)."""
    value = entity.get(key)
    return tuple(str(item) for item in value) if isinstance(value, tuple) else ()


def _spans(entity: dict[str, object]) -> dict[str, str]:
    """The reducer's ``operator_spans`` pairs of (correcting row id, span), by row id."""
    value = entity.get("operator_spans")
    if not isinstance(value, tuple):
        return {}
    return {str(pair[0]): str(pair[1]) for pair in value if len(pair) == 2}


def _is_send(event: ModelLedgerRowEventBase) -> bool:
    return _SENT_ON_BEHALF in event.raw_row or cell(event, "sent") is not None


class _Deriver:
    def __init__(self, view: LedgerView, request: ModelGroundTruthRequest) -> None:
        self.view = view
        self.request = request
        self.window_start = parse_stamp(request.window_start)
        self.session_lanes = frozenset(
            (*request.orchestrator_lanes, *request.extra_lanes)
        )
        self.family = self._family()
        self.items: list[ModelGroundTruthItem] = []

    def _family(self) -> frozenset[str]:
        family = set(self.session_lanes)
        orchestrators = set(self.request.orchestrator_lanes)
        for event in self.view.events:
            if (
                isinstance(event, ModelLedgerClaimEvent | ModelLedgerMsgEvent)
                and cell(event, "parent") in orchestrators
            ):
                lane = event.row_lane or (
                    event.sender if isinstance(event, ModelLedgerMsgEvent) else None
                )
                if lane:
                    family.add(lane)
        return frozenset(family)

    def in_window(self, event: ModelLedgerRowEventBase) -> bool:
        return self.window_start <= parse_stamp(event.row_timestamp) <= self.view.now

    def add(
        self,
        kind: K,
        key: str,
        quote: str,
        spans: tuple[str, ...],
        rows: list[ModelGroundTruthRow],
    ) -> None:
        if spans:
            self.items.append(
                ModelGroundTruthItem(
                    kind=kind,
                    key=f"{kind.value}|{key}",
                    quote=quote,
                    required_spans=spans,
                    expected_conduct=EXPECTED_CONDUCT[kind],
                    rows=tuple(rows),
                )
            )

    # RULING and OPERATOR_CONSENT --------------------------------------------------

    def _entity_event(self, key: str) -> ModelLedgerRowEventBase:
        return self.view.by_id[str(self.view.entities[key]["opened_row_id"])]

    def decisions(self, prefix: str, kind: K) -> None:
        entities = self.view.entities
        spent = (
            set(self.request.spent_consent_stamps)
            if kind is K.OPERATOR_CONSENT
            else set()
        )
        live = {
            key
            for key, entity in entities.items()
            if key.startswith(prefix)
            and entity["is_open"]
            and self._entity_event(key).row_timestamp not in spent
        }
        by_row = {str(entities[key]["opened_row_id"]): key for key in live}
        parent = {key: key for key in live}

        def find(key: str) -> str:
            while parent[key] != key:
                parent[key] = parent[parent[key]]
                key = parent[key]
            return key

        for key in live:
            for relation in ("amended_by", "extended_by"):
                for row_id in _ids(entities[key], relation):
                    other = by_row.get(row_id)
                    if other is not None:
                        parent[find(other)] = find(key)
        groups: dict[str, list[str]] = {}
        for key in live:
            groups.setdefault(find(key), []).append(key)
        for members in groups.values():
            events = sorted(
                ((self._entity_event(k), k) for k in members),
                key=lambda pair: (pair[0].row_timestamp, pair[1]),
            )
            if not any(self.in_window(event) for event, _ in events):
                continue
            amenders = {
                row_id for _, k in events for row_id in _ids(entities[k], "amended_by")
            }
            rows: list[ModelGroundTruthRow] = []
            for index, (event, _) in enumerate(events):
                role = (
                    "primary"
                    if index == 0
                    else "amender"
                    if event.row_id in amenders
                    else "extender"
                )
                rows.append(_row(event, role))
            seen = {r.row_id for r in rows}
            for _event, k in events:
                spans_by_row = _spans(entities[k])
                for row_id in _ids(entities[k], "corrected_by"):
                    if row_id not in seen and row_id in self.view.by_id:
                        seen.add(row_id)
                        rows.append(
                            _row(
                                self.view.by_id[row_id],
                                "correction",
                                spans_by_row.get(row_id),
                            )
                        )
            self.add(
                kind,
                str(events[0][1]),
                "\n".join(r.quote for r in rows),
                tuple(dict.fromkeys(r.stamp for r in rows)),
                rows,
            )

    # State kinds --------------------------------------------------------------------

    def open_claims(self) -> None:
        closed = {
            (event.row_lane, event.closes)
            for event in self.view.events
            if isinstance(event, ModelLedgerTerminalEvent) and event.closes
        }
        for key, entity in self.view.entities.items():
            lane = entity["lane"]
            if not key.startswith("claim:") or not entity["is_open"]:
                continue
            if lane not in self.family:
                continue
            event = self._entity_event(key)
            if (lane, event.row_timestamp) in closed:
                continue
            self.add(
                K.OPEN_CLAIM,
                key,
                event.free_text or event.raw_row,
                (str(lane),),
                [_row(event, "primary")],
            )

    def holds(self) -> None:
        for key, entity in self.view.entities.items():
            if not key.startswith("hold:") or not entity["is_open"]:
                continue
            targets = set(split_list(str(entity["scope_to"] or "")))
            if entity["lane"] not in self.family and not targets & self.family:
                continue
            event = self._entity_event(key)
            self.add(
                K.HOLD,
                key,
                event.free_text or event.raw_row,
                (event.row_timestamp,),
                [_row(event, "primary")],
            )

    def unacked_messages(self) -> None:
        for key, entity in self.view.entities.items():
            if not key.startswith("msg:") or not entity["is_open"]:
                continue
            event = self._entity_event(key)
            if not isinstance(event, ModelLedgerMsgEvent) or _is_send(event):
                continue
            if not set(event.recipients) & self.session_lanes:
                continue
            self.add(
                K.UNACKED_MSG,
                key,
                event.free_text or event.raw_row,
                (event.row_timestamp,),
                [_row(event, "primary")],
            )

    # Kinds the fold does not model ----------------------------------------------------

    def open_asks(self) -> None:
        operator = set(self.request.operator_names)
        events = self.view.events
        for index, ask in enumerate(events):
            if not isinstance(ask, ModelLedgerMsgEvent) or _is_send(ask):
                continue
            if ask.sender not in self.family or not set(ask.recipients) & operator:
                continue
            qkey = cell(ask, "qkey")
            names = {name for name in (ask.msg_id, ask.row_timestamp) if name}
            answered = False
            for later in events[index + 1 :]:
                if later.row_type.value not in ("RULING", "ACK", "MSG"):
                    continue
                if (
                    isinstance(later, ModelLedgerMsgEvent)
                    and later.sender in self.family
                    and set(later.recipients) & operator
                ):
                    continue  # a family lane asking again is not an answer
                if cell(later, "re") in names or (qkey and cell(later, "qkey") == qkey):
                    answered = True
                    break
            if not answered:
                self.add(
                    K.OPEN_ASK,
                    ask.msg_id or f"{ask.row_timestamp}:{ask.sender}",
                    ask.free_text or ask.raw_row,
                    (ask.row_timestamp,),
                    [_row(ask, "primary")],
                )

    def promises(self) -> None:
        for event in self.view.events:
            if event.row_lane not in self.family and not (
                isinstance(event, ModelLedgerMsgEvent) and event.sender in self.family
            ):
                continue
            promise = cell(event, "promise")
            sent = (
                isinstance(event, ModelLedgerMsgEvent)
                and _SENT_ON_BEHALF in event.raw_row
            )
            if not (sent or promise) or not self.in_window(event):
                continue
            text = event.free_text
            if _SENT_ON_BEHALF in text:
                text = text.split(_SENT_ON_BEHALF, 1)[1]
            spans = content_spans(promise or text) or (event.row_timestamp,)
            key = (
                event.msg_id
                if isinstance(event, ModelLedgerMsgEvent) and event.msg_id
                else f"{event.row_timestamp}:{event.row_lane}"
            )
            self.add(
                K.PROMISE_SENT,
                key,
                promise or event.free_text or event.raw_row,
                spans,
                [_row(event, "primary")],
            )

    def drafts(self) -> None:
        events = self.view.events
        seen: set[str] = set()
        for index, event in enumerate(events):
            path = cell(event, "draft")
            if (
                not path
                or path in seen
                or event.row_lane not in self.family
                or _is_send(event)
                or not self.in_window(event)
            ):
                continue
            seen.add(path)
            thread = cell(event, "thread")
            names = [name for name in (path, thread) if name]
            sent = any(
                _is_send(later) and any(name in later.raw_row for name in names)
                for later in events[index + 1 :]
            )
            if not sent:
                self.add(
                    K.DRAFT_UNSENT,
                    path,
                    event.free_text or event.raw_row,
                    (path,),
                    [_row(event, "primary")],
                )


class HandlerCompactionGroundTruth:
    """Derive the must-survive set from a pinned ledger prefix at a boundary instant."""

    def handle(self, request: ModelGroundTruthRequest) -> ModelGroundTruthResult:
        now = parse_stamp(request.now)
        view = build_view(request.ledger_prefix, now)
        deriver = _Deriver(view, request)
        deriver.decisions("ruling:", K.RULING)
        deriver.decisions("consent:", K.OPERATOR_CONSENT)
        deriver.open_claims()
        deriver.holds()
        deriver.open_asks()
        deriver.promises()
        deriver.drafts()
        deriver.unacked_messages()
        items = tuple(
            sorted(
                deriver.items,
                key=lambda item: (
                    _KIND_ORDER[item.kind],
                    item.rows[0].stamp,
                    item.key,
                ),
            )
        )
        counts = tuple(
            ModelKindCount(kind=kind, count=sum(1 for i in items if i.kind is kind))
            for kind in K
        )
        return ModelGroundTruthResult(
            session_id=request.session_id,
            now=request.now,
            window_start=request.window_start,
            lane_family=tuple(sorted(deriver.family)),
            prefix_row_count=view.prefix_row_count,
            rows_as_of_count=len(view.events),
            refused_row_count=view.refused_row_count,
            prefix_hash=view.prefix_hash,
            items=items,
            counts_by_kind=counts,
            set_hash=_set_hash(request, items),
        )


def _set_hash(
    request: ModelGroundTruthRequest, items: tuple[ModelGroundTruthItem, ...]
) -> str:
    body = json.dumps(
        {
            "now": request.now,
            "window_start": request.window_start,
            "items": [item.model_dump(mode="json") for item in items],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()
