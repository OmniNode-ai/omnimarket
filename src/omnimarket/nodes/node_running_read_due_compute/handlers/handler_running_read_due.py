# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The running-read due row at merge, and the alarm when no run line arrives (OMN-26491).

Nobody owned the read of a merged change running. A merge of a runtime-affecting pull request now
writes one STATUS row, ``kind=running-read-due``, due at merge plus 60 minutes, carrying the ticket,
its owner and the reading the pull request body names. A ``Running read:`` line of the body names the
reading as ``node=<n> topic=<t>``, ``unit=<u> host=<h>`` or ``overlay=<o> reader=<r>``; a body that
names none still writes the row, with ``reading=unnamed``, because an unread change is the case the
row exists for.

The row closes only when a later STATUS row cites its id as ``due_id=`` and carries both ``observed=``
and ``read_at=`` (the ledger grammar has no DUE row type, so the due row is a STATUS row with
``kind=``). A due row still open at its due time raises one MSG to the owner; the alarm is keyed on the
due id, so a later read finds it and raises none. Every ticket with an open due row, before or after
its due time, is held out of Done.

Pure: no file, clock, bus or model call. The caller supplies the merge, the ledger text and the time.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, uuid5

from omnimarket.handlers import ledger_row_grammar as grammar
from omnimarket.models.work_ledger_append import ModelWorkLedgerAppendRequest
from omnimarket.nodes.node_running_read_due_compute.models.model_running_read_due import (
    ModelMergedPrFact,
    ModelRunningReadAlarm,
    ModelRunningReadDueRequest,
    ModelRunningReadDueResult,
)

LANE = "landing-controller"
DUE_AFTER = timedelta(minutes=60)
KIND_DUE = "running-read-due"
KIND_ALARM = "running-read-alarm"
UNNAMED = "unnamed"

_READING_LINE_RE = re.compile(
    r"^[ \t]*running read:[ \t]*(?P<named>.+?)[ \t]*$", re.I | re.M
)
_TOKEN_RE = re.compile(r"^(?P<key>[a-z]+)=(?P<value>[A-Za-z0-9][A-Za-z0-9._:/@-]*)$")
_READING_FORMS = (("node", "topic"), ("unit", "host"), ("overlay", "reader"))


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def reading_named_by(pr_body: str) -> str:
    """The reading a body names, as one pipe-free cell value, else ``unnamed``."""
    for match in _READING_LINE_RE.finditer(pr_body):
        pairs: dict[str, str] = {}
        for token in match.group("named").split():
            token_match = _TOKEN_RE.match(token)
            if token_match is None:
                pairs = {}
                break
            pairs[token_match.group("key").lower()] = token_match.group("value")
        for form in _READING_FORMS:
            if set(pairs) == set(form):
                return ",".join(f"{key}:{pairs[key]}" for key in form)
    return UNNAMED


def due_id_for(pr: str) -> str:
    repo, number = pr.split("#", 1)
    return f"rrd:{repo}:{number}"


class HandlerRunningReadDue:
    """Stateless: map one merge to its due row, or read the ledger text for what is owed."""

    def handle(self, request: ModelRunningReadDueRequest) -> ModelRunningReadDueResult:
        if request.operation == "write_due_row":
            return self._write_due_row(request.merged)
        return self._read_due_rows(request)

    def _write_due_row(
        self, merged: ModelMergedPrFact | None
    ) -> ModelRunningReadDueResult:
        if merged is None:
            return ModelRunningReadDueResult(
                operation="write_due_row",
                refusal="write_due_row needs the merged pull request",
            )
        if not merged.runtime_affecting:
            return ModelRunningReadDueResult(operation="write_due_row")
        if merged.ticket is None:
            return ModelRunningReadDueResult(
                operation="write_due_row",
                refusal=f"{merged.pr} is runtime-affecting and names no ticket to hold out of Done",
            )
        due_id = due_id_for(merged.pr)
        row = " | ".join(
            [
                _stamp(merged.merged_at),
                "STATUS",
                f"lane={LANE}",
                "src=landing",
                f"kind={KIND_DUE}",
                f"id={due_id}",
                f"ticket={merged.ticket}",
                f"pr={merged.pr}",
                f"owner={merged.owner}",
                f"due={_stamp(merged.merged_at + DUE_AFTER)}",
                f"reading={reading_named_by(merged.pr_body)}",
                "via=bus:landing",
                "A running read of this merge is due; the ticket stays out of Done until a STATUS row "
                f"cites due_id={due_id} with observed= and read_at=",
            ]
        )
        return ModelRunningReadDueResult(
            operation="write_due_row",
            due_id=due_id,
            append_request=ModelWorkLedgerAppendRequest(
                request_id=uuid5(NAMESPACE_URL, f"onex:running-read-due:{due_id}"),
                rows=row,
                requested_by_lane=LANE,
                requesting_host="landing-bus",
                requested_at=merged.merged_at,
                append_if_absent_row_id=due_id,
            ),
        )

    def _read_due_rows(
        self, request: ModelRunningReadDueRequest
    ) -> ModelRunningReadDueResult:
        if request.now is None:
            return ModelRunningReadDueResult(
                operation="read_due_rows",
                refusal="read_due_rows needs the time to read at",
            )
        due_rows: dict[str, grammar.Row] = {}
        closed: set[str] = set()
        alarmed: set[str] = set()
        for line in request.ledger_text.splitlines():
            if not grammar.opens_a_row(line):
                continue
            row = grammar.parse_row(line, types=frozenset({"STATUS", "MSG"}))
            if row is None:
                continue
            if row.row_type == "STATUS" and row.value("kind") == KIND_DUE:
                row_id = row.token("id")
                if row_id is not None:
                    due_rows.setdefault(row_id, row)
            elif row.row_type == "STATUS":
                cited = row.token("due_id")
                if cited and row.value("observed") and row.value("read_at"):
                    closed.add(cited)
            elif row.value("kind") == KIND_ALARM:
                cited = row.token("re")
                if cited:
                    alarmed.add(cited)
        open_rows = {key: row for key, row in due_rows.items() if key not in closed}
        alarms: list[ModelRunningReadAlarm] = []
        for due_id, row in open_rows.items():
            due = _parse_stamp(row.value("due"))
            ticket, owner = row.token("ticket"), row.token("owner")
            if due is None or ticket is None or owner is None:
                continue
            if request.now < due or due_id in alarmed:
                continue
            alarms.append(_alarm(due_id, ticket, owner, _stamp(due), request.now))
        tickets = sorted({t for r in open_rows.values() if (t := r.token("ticket"))})
        return ModelRunningReadDueResult(
            operation="read_due_rows",
            open_due_ids=sorted(open_rows),
            alarms=alarms,
            blocked_tickets=tickets,
            blocked_reason="; ".join(
                f"{ticket} stays out of Done: a running-read due row is open until a STATUS row "
                "carries observed= and read_at="
                for ticket in tickets
            ),
        )


def _parse_stamp(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None


def _alarm(
    due_id: str, ticket: str, owner: str, due: str, now: datetime
) -> ModelRunningReadAlarm:
    stamp = _stamp(now)
    row = " | ".join(
        [
            stamp,
            "MSG",
            f"from={LANE}",
            f"to={owner}",
            f"id={stamp}-{LANE}",
            f"ticket={ticket}",
            f"re={due_id}",
            f"kind={KIND_ALARM}",
            f"due={due}",
            "via=bus:landing",
            f"No run line by {due}: {ticket} stays out of Done until a STATUS row cites "
            f"due_id={due_id} with observed= and read_at=",
        ]
    )
    return ModelRunningReadAlarm(
        due_id=due_id,
        ticket=ticket,
        owner=owner,
        append_request=ModelWorkLedgerAppendRequest(
            # Keyed on the due row: a redelivered alarm is the same request.
            request_id=uuid5(NAMESPACE_URL, f"onex:running-read-alarm:{due_id}"),
            rows=row,
            requested_by_lane=LANE,
            requesting_host="landing-bus",
            requested_at=now,
        ),
    )
