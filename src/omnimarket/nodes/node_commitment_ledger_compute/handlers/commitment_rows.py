# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The commitment row: how an ask is written to the ledger and how the rows fold back into commitments.

A commitment is a STATUS row of kind commitment (the type and the kind's spelling are the overlay's), so the ledger
grammar admits it with no grammar change. Three cells carry it: ``ask`` (the operator's words, percent encoded for the
four characters a ledger cell cannot hold), ``owner`` and ``check`` (an argv line run with no shell). Its first row
declares ``commitment=<its own id>`` and ``status=open``; every later row of the same ``commitment=`` id is a reading by
the check runner and carries ``status=verified`` or ``status=regressed`` and the check's ``exit=``. The newest reading
wins. Pure functions: no clock, no file.
"""

from __future__ import annotations

import shlex
from collections import defaultdict
from datetime import UTC, datetime

from omnimarket.handlers import ledger_row_grammar as grammar
from omnimarket.models.model_reading import ModelLedgerEvidenceRow
from omnimarket.nodes.node_commitment_ledger_compute.models import (
    EnumCommitmentStatus,
    ModelCommitment,
    ModelCommitmentRules,
)

ASK_CELL = "ask"
OWNER_CELL = "owner"
CHECK_CELL = "check"
STATUS_CELL = "status"
ID_CELL = "commitment"
EXIT_CELL = "exit"
TICKET_CELL = "ticket"
REQUIRES_CELL = "requires"

# The characters a ledger cell cannot hold verbatim, and the cell's own quote and escape.
_ENCODE = (("%", "%25"), ("|", "%7C"), ('"', "%22"), ("\n", "%0A"), ("\r", "%0D"))
_STAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def encode_ask(ask: str) -> str:
    out = ask
    for raw, encoded in _ENCODE:
        out = out.replace(raw, encoded)
    return out


def decode_ask(cell: str) -> str:
    out = cell.strip()
    if len(out) >= 2 and out[0] == '"' and out[-1] == '"':
        out = out[1:-1]
    for raw, encoded in reversed(_ENCODE):
        out = out.replace(encoded, raw)
    return out


def stamp_of(now: datetime) -> str:
    return now.astimezone(UTC).strftime(_STAMP_FORMAT)


def commitment_id(now: datetime, lane: str) -> str:
    return f"{stamp_of(now)}-{lane}"


def open_row_refusals(
    rules: ModelCommitmentRules,
    lane: str,
    ask: str,
    owner: str,
    check: str,
    requires: tuple[str, ...],
) -> list[str]:
    """Every reason these cells do not make a commitment. Empty when they do."""
    reasons: list[str] = []
    if not grammar.LANE_TOKEN_RE.match(lane):
        reasons.append(f"lane={lane!r} is not a lane token")
    if not ask.strip():
        reasons.append("the ask is empty: record the operator's words verbatim")
    elif len(ask) > rules.max_ask_chars:
        reasons.append(f"the ask is over {rules.max_ask_chars} characters")
    if not grammar.LANE_TOKEN_RE.match(owner):
        reasons.append(f"owner={owner!r} is not a lane token: name who owes it")
    if not check.strip():
        reasons.append("the check is empty: a commitment needs a runnable check")
    elif len(check) > rules.max_check_chars:
        reasons.append(f"the check is over {rules.max_check_chars} characters")
    elif "|" in check or "\n" in check or "\r" in check:
        reasons.append(
            "the check holds a pipe or a newline: a check is one argv line run with no shell "
            "(a ledger cell cannot hold a pipe); put the pipeline in a command the argv names"
        )
    else:
        try:
            if not shlex.split(check):
                reasons.append("the check has no argv")
        except ValueError as exc:
            reasons.append(f"the check does not split into argv: {exc}")
    for ref in requires:
        if not ref.strip() or "," in ref or "|" in ref:
            reasons.append(f"requires entry {ref!r} is not one reference")
    return reasons


def open_row(
    rules: ModelCommitmentRules,
    now: datetime,
    lane: str,
    ask: str,
    owner: str,
    check: str,
    ticket: str | None,
    requires: tuple[str, ...],
) -> str:
    cells = [
        f"lane={lane}",
        f"{rules.kind_cell}={rules.kind_value}",
        f"{ID_CELL}={commitment_id(now, lane)}",
        f"{STATUS_CELL}={EnumCommitmentStatus.OPEN.value}",
        f"{OWNER_CELL}={owner}",
    ]
    if ticket:
        cells.append(f"{TICKET_CELL}={ticket}")
    if requires:
        cells.append(f"{REQUIRES_CELL}={','.join(requires)}")
    cells.append(f"{CHECK_CELL}={check}")
    cells.append(f'{ASK_CELL}="{encode_ask(ask)}"')
    text = "An operator ask recorded as a commitment: open until its check passes on the running system."
    return " | ".join((stamp_of(now), rules.row_type, *cells, text))


def result_row(
    rules: ModelCommitmentRules,
    now: datetime,
    lane: str,
    cid: str,
    status: EnumCommitmentStatus,
    exit_code: int,
) -> str:
    cells = [
        f"lane={lane}",
        f"{rules.kind_cell}={rules.kind_value}",
        f"{ID_CELL}={cid}",
        f"{STATUS_CELL}={status.value}",
        f"{EXIT_CELL}={exit_code}",
    ]
    text = f"The commitment's check read {status.value} (exit {exit_code})."
    return " | ".join((stamp_of(now), rules.row_type, *cells, text))


def _cell_values(row: ModelLedgerEvidenceRow, key: str) -> str | None:
    """The first value of a cell, read from the row's text so a value holding `=` or spaces is whole."""
    for part in row.raw.split(" | ")[2:]:
        name, sep, value = part.partition("=")
        if sep and name.strip() == key and value.strip():
            return value.strip()
    return None


def fold_commitments(
    rows: tuple[ModelLedgerEvidenceRow, ...], rules: ModelCommitmentRules
) -> tuple[list[ModelCommitment], list[str]]:
    """Every commitment the rows declare with its newest reading, and each commitment-kind row that did not read."""
    kind_rows = sorted(
        (
            r
            for r in rows
            if r.row_type == rules.row_type
            and r.cells.get(rules.kind_cell) == rules.kind_value
        ),
        key=lambda r: (r.stamp, r.raw),
    )
    malformed: list[str] = []
    opened: dict[str, ModelCommitment] = {}
    readings: dict[str, list[ModelLedgerEvidenceRow]] = defaultdict(list)
    pending_readings: list[ModelLedgerEvidenceRow] = []
    for r in kind_rows:
        cid = _cell_values(r, ID_CELL)
        if cid is None or grammar.ID_RE.match(cid) is None:
            malformed.append(f"{r.stamp:{_STAMP_FORMAT}}: no valid {ID_CELL}= id")
            continue
        if ASK_CELL in r.cells or _cell_values(r, ASK_CELL) is not None:
            problems = []
            ask_cell = _cell_values(r, ASK_CELL)
            owner = _cell_values(r, OWNER_CELL)
            check = _cell_values(r, CHECK_CELL)
            if not ask_cell or not decode_ask(ask_cell).strip():
                problems.append(ASK_CELL)
            if not owner:
                problems.append(OWNER_CELL)
            if not check:
                problems.append(CHECK_CELL)
            if _cell_values(r, STATUS_CELL) != EnumCommitmentStatus.OPEN.value:
                problems.append(f"{STATUS_CELL}=open")
            if cid in opened:
                problems.append("duplicate id")
            if problems:
                malformed.append(f"{cid}: opening row lacks {', '.join(problems)}")
                continue
            requires = _cell_values(r, REQUIRES_CELL) or ""
            opened[cid] = ModelCommitment(
                commitment_id=cid,
                ask=decode_ask(ask_cell or ""),
                owner=owner or "",
                check=check or "",
                ticket=_cell_values(r, TICKET_CELL),
                requires=tuple(p.strip() for p in requires.split(",") if p.strip()),
                status=EnumCommitmentStatus.OPEN,
                opened_at=r.stamp,
            )
        else:
            pending_readings.append(r)
    for r in pending_readings:
        cid = _cell_values(r, ID_CELL) or ""
        status = _cell_values(r, STATUS_CELL)
        exit_text = _cell_values(r, EXIT_CELL)
        if cid not in opened:
            malformed.append(f"{cid}: reading for a commitment no row opened")
            continue
        if (
            status
            not in (
                EnumCommitmentStatus.VERIFIED.value,
                EnumCommitmentStatus.REGRESSED.value,
            )
            or exit_text is None
            or not exit_text.lstrip("-").isdigit()
        ):
            malformed.append(f"{cid}: reading with status={status} exit={exit_text}")
            continue
        readings[cid].append(r)
    folded: list[ModelCommitment] = []
    for cid, commitment in opened.items():
        update: dict[str, object] = {}
        mine = readings.get(cid, [])
        if mine:
            newest = mine[-1]
            update = {
                "status": EnumCommitmentStatus(_cell_values(newest, STATUS_CELL) or ""),
                "last_checked_at": newest.stamp,
                "last_exit": int(_cell_values(newest, EXIT_CELL) or "0"),
                "regressions": sum(
                    1
                    for m in mine
                    if _cell_values(m, STATUS_CELL)
                    == EnumCommitmentStatus.REGRESSED.value
                ),
            }
        folded.append(commitment.model_copy(update=update))
    folded.sort(key=lambda c: (c.opened_at, c.commitment_id))
    return folded, malformed


__all__ = [
    "decode_ask",
    "encode_ask",
    "fold_commitments",
    "open_row",
    "open_row_refusals",
    "result_row",
]
