# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Pure parser from one rolling-ledger row to its typed event (OMN-19513).

Row shape (``ledger-row-grammar/2``)::

    <YYYY-MM-DDTHH:MM:SSZ> | <TYPE> | key=value | ... | free text

A pipe cell that starts ``key=`` is a field; any other cell is free text. The
parser does not judge grammar rules (the append path did that); it refuses only
what it cannot type: a row with no stamp, and a type outside the canonical set.
It is deterministic and does no I/O, so it is falsifiable by a unit test.
"""

from __future__ import annotations

import re

from omnimarket.events.enum_ledger_row_type import (
    EnumLedgerRowType,
)
from omnimarket.events.model_ledger_row_event import (
    DEFAULT_LEDGER_ID,
    EVENT_MODEL_BY_TYPE,
    ModelLedgerCell,
    ModelLedgerRowEventBase,
    work_ledger_row_id,
)

_STAMP = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z"
_ROW_RE = re.compile(
    rf"^(?P<stamp>{_STAMP}) \| (?P<type>[^|]*?)\s*(?:\|(?P<rest>.*))?$", re.DOTALL
)
_CELL_RE = re.compile(r"^(?P<key>[A-Za-z][A-Za-z0-9_-]*)=(?P<value>.*)$", re.DOTALL)
_APPROVED_SCOPE = "APPROVED SCOPE:"
_OUT_OF_SCOPE = "OUT OF SCOPE:"


class LedgerRowRefusalError(ValueError):
    """The row cannot be typed: no stamp, or a type outside the canonical set."""


def row_id_of(raw_row: str) -> str:
    """The content hash that identifies a row: sha256 of the trimmed raw row."""
    return work_ledger_row_id(raw_row)


def _split_cells(rest: str) -> list[str]:
    return [cell.strip() for cell in rest.split("|")]


def _split_list(value: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in value.split(",") if part.strip())


def parse_ledger_row(
    raw_row: str,
    *,
    ledger_id: str = DEFAULT_LEDGER_ID,
    source: str = "onex-ledger",
) -> ModelLedgerRowEventBase:
    """Parse one ledger row into the typed event for its row type."""
    raw = raw_row.strip()
    match = _ROW_RE.match(raw)
    if match is None:
        raise LedgerRowRefusalError("row does not open with '<UTC stamp> | <TYPE>'")
    type_cell = match.group("type").strip()
    try:
        row_type = EnumLedgerRowType(type_cell)
    except ValueError:
        raise LedgerRowRefusalError(
            f"row type {type_cell!r} is not a canonical ledger type"
        ) from None

    cells: list[ModelLedgerCell] = []
    free: list[str] = []
    for cell in _split_cells(match.group("rest") or ""):
        if not cell:
            continue
        keyed = _CELL_RE.match(cell)
        if keyed is not None:
            cells.append(
                ModelLedgerCell(
                    key=keyed.group("key"), value=keyed.group("value").strip()
                )
            )
        else:
            free.append(cell)

    def one(key: str) -> str | None:
        for cell in cells:
            if cell.key == key:
                return cell.value
        return None

    def many(key: str) -> tuple[str, ...]:
        return tuple(cell.value for cell in cells if cell.key == key)

    tickets = _split_list(",".join((*many("ticket"), *many("tickets"))))
    fields: dict[str, object] = {
        "ledger_id": ledger_id,
        "row_id": row_id_of(raw),
        "row_timestamp": match.group("stamp"),
        "row_lane": one("lane") or one("from"),
        "tickets": tickets,
        "cells": tuple(cells),
        "free_text": " | ".join(free),
        "raw_row": raw,
        "source": source,
    }

    if row_type is EnumLedgerRowType.CLAIM:
        fields.update(
            repo=one("repo"), pr=one("pr"), parent=one("parent"), consent=one("consent")
        )
    elif row_type is EnumLedgerRowType.STATUS:
        fields.update(
            repo=one("repo"),
            pr=one("pr"),
            head=one("head"),
            state=one("state"),
            claim=one("claim"),
        )
    elif row_type is EnumLedgerRowType.TERMINAL:
        fields.update(
            outcome=one("outcome"),
            friction=one("friction"),
            pr=one("pr"),
            closes=one("closes") or one("closes-CLAIM"),
        )
    elif row_type is EnumLedgerRowType.HOLD:
        fields.update(
            hold_id=one("id"),
            scope_to=one("to"),
            scope_repo=one("repo"),
            scope_pr=one("pr"),
            scope_surface=one("surface"),
            until=one("until"),
            after=one("after"),
            release_condition=one("release") or one("proof"),
        )
    elif row_type is EnumLedgerRowType.RELEASE:
        fields.update(re=one("re"), surface=one("surface"), result=one("result"))
    elif row_type in (EnumLedgerRowType.MSG, EnumLedgerRowType.ACK):
        fields.update(
            sender=one("from"),
            recipients=_split_list(one("to") or ""),
            msg_id=one("id"),
            re=one("re"),
        )
        if row_type is EnumLedgerRowType.MSG:
            fields.update(repo=one("repo"), pr=one("pr"))
    elif row_type is EnumLedgerRowType.RULING:
        fields.update(
            amends=many("amends"),
            supersedes=many("supersedes"),
            approved_by=one("approved_by"),
        )
    elif row_type is EnumLedgerRowType.OPERATOR_CONSENT:
        scope = next((c for c in free if c.startswith(_APPROVED_SCOPE)), None)
        out = next((c for c in free if c.startswith(_OUT_OF_SCOPE)), None)
        fields.update(
            approved_scope=scope[len(_APPROVED_SCOPE) :].strip() if scope else None,
            out_of_scope=out[len(_OUT_OF_SCOPE) :].strip() if out else None,
            approved_by=one("approved_by"),
        )
    elif row_type is EnumLedgerRowType.FRICTION:
        fields.update(cost=one("cost"), friction_class=one("class"))
    elif row_type is EnumLedgerRowType.CORRECTION:
        fields.update(
            corrects=(*many("corrects"), *many("amends"), *many("supersedes"))
        )

    return EVENT_MODEL_BY_TYPE[row_type](**fields)


__all__: list[str] = ["LedgerRowRefusalError", "parse_ledger_row", "row_id_of"]
