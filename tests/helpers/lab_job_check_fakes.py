# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Fakes shared by the lab job check tests: the ledger window, a store, a bus and a pool.

The ledger rows under ``tests/fixtures/lab_job_check`` are verbatim rows of the
rolling work ledger for 2026-10-04T18:00Z to 2026-10-05T13:00Z. The hook
aggregates the tests pass in are constructed: ``public.hook_events`` is not
readable from a lane.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from omnimarket.enums.enum_lab_job import EnumLabJobKind, EnumLabJobState
from omnimarket.enums.enum_lab_job_check import EnumLabJobCheckScope
from omnimarket.models.lab_job import (
    ModelLabJobDoneCriterion,
    ModelLabJobRow,
    ModelLabJobSpec,
)
from omnimarket.models.lab_job.model_lab_job_check import (
    ModelLabJobChecked,
    ModelLabJobCheckRequested,
    ModelLabJobPrObservation,
)
from omnimarket.nodes.node_lab_job_check_effect.handlers.handler_lab_job_check_effect import (
    HandlerLabJobCheckEffect,
)
from omnimarket.nodes.node_lab_job_check_effect.handlers.lane_evidence import row_cell
from omnimarket.nodes.node_lab_job_check_effect.models import (
    ModelLaneActivityReading,
    ModelLedgerRowReading,
    ModelRelayReading,
)

_S = EnumLabJobState
FIXTURE = Path(__file__).parents[1] / "fixtures" / "lab_job_check"


def _at(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def _ledger_lines() -> list[str]:
    return [
        line
        for line in (FIXTURE / "ledger_window_2026-10-05.txt").read_text().splitlines()
        if line and not line.startswith("#")
    ]


def _readings() -> list[ModelLedgerRowReading]:
    out = []
    for line in _ledger_lines():
        cells = line.split(" | ")
        lane = row_cell(line, "lane")
        out.append(
            ModelLedgerRowReading(
                row_id=hashlib.sha256(line.encode()).hexdigest(),
                row_ts=_at(cells[0]),
                row_lane=lane,
                raw_row=line,
            )
        )
    return out


def _job(
    job_id: str,
    state: EnumLabJobState,
    *,
    entered: str = "2026-10-05T00:00:00Z",
    claimed: str | None = None,
    run_id: str | None = None,
    ticket: str | None = None,
    kind: EnumLabJobKind = EnumLabJobKind.ADOPTED,
    spec: ModelLabJobSpec | None = None,
    next_dispatch: str | None = None,
    alert_sent: str | None = None,
) -> ModelLabJobRow:
    return ModelLabJobRow(
        job_id=job_id,
        kind=kind,
        state=state,
        spec=spec,
        entered_state_at=_at(entered),
        claimed_at=_at(claimed) if claimed else None,
        run_id=run_id,
        ticket=ticket,
        next_dispatch_at=_at(next_dispatch) if next_dispatch else None,
        alert_sent_at=_at(alert_sent) if alert_sent else None,
    )


class FakeStore:
    """Answers the store protocol from the fixture ledger and constructed hook readings."""

    def __init__(
        self,
        jobs: Sequence[ModelLabJobRow],
        *,
        activity: Mapping[str, ModelLaneActivityReading] | None = None,
        relay: ModelRelayReading | None = None,
        prs: Mapping[str, ModelLabJobPrObservation] | None = None,
    ) -> None:
        self._jobs = tuple(jobs)
        self._activity = dict(activity or {})
        self._relay = relay or ModelRelayReading()
        self._prs = dict(prs or {})
        self._rows = _readings()
        #: Rows stamped after this instant do not exist yet, as in the live table.
        self.as_of: datetime | None = None

    async def open_jobs(self) -> tuple[ModelLabJobRow, ...]:
        return self._jobs

    async def claim_row(self, job: ModelLabJobRow) -> ModelLedgerRowReading | None:
        for row in self._rows:
            if " | CLAIM | " not in row.raw_row:
                continue
            if job.run_id and row_cell(row.raw_row, "run") == job.run_id:
                return row
            if (
                not job.run_id
                and job.claimed_at == row.row_ts
                and job.ticket == row_cell(row.raw_row, "ticket")
            ):
                return row
        return None

    async def terminal_row_after(
        self, lane: str, claimed_at: datetime
    ) -> ModelLedgerRowReading | None:
        later = [
            row
            for row in self._rows
            if " | TERMINAL | " in row.raw_row
            and row.row_lane == lane
            and row.row_ts > claimed_at
            and (self.as_of is None or row.row_ts <= self.as_of)
        ]
        return max(later, key=lambda row: row.row_ts) if later else None

    async def lane_activity(
        self, claims: Mapping[str, datetime], until: datetime
    ) -> Mapping[str, ModelLaneActivityReading]:
        return {lane: r for lane, r in self._activity.items() if lane in claims}

    async def relay(self, since: datetime, until: datetime) -> ModelRelayReading:
        return self._relay

    async def pr_states(
        self, targets: Sequence[str]
    ) -> Mapping[str, ModelLabJobPrObservation]:
        return {t: self._prs[t] for t in targets if t in self._prs}


def _request(now: str) -> ModelLabJobCheckRequested:
    return ModelLabJobCheckRequested(
        check_id="lab-job-check-test",
        scope=EnumLabJobCheckScope.NONTERMINAL,
        requested_at=_at(now),
    )


async def _sweep(
    store: FakeStore, now: str, bus: Any = None
) -> dict[str, ModelLabJobChecked]:
    store.as_of = _at(now)
    sweep = await HandlerLabJobCheckEffect(store, bus=bus).handle(_request(now))
    return {record.job_id: record for record in sweep.checked}


ADOPTED = {
    "dod": _job(
        "lj-" + "d" * 16,
        _S.RUNNING,
        claimed="2026-10-05T00:47:39Z",
        run_id="2026-10-05T0045Z",
        ticket="OMN-17427",
    ),
    "fan": _job(
        "lj-" + "f" * 16,
        _S.RUNNING,
        claimed="2026-10-05T04:56:07Z",
        ticket="OMN-17427",
    ),
    "validator": _job(
        "lj-" + "a" * 16,
        _S.RUNNING,
        claimed="2026-10-05T05:23:52Z",
        ticket="OMN-20568",
    ),
    "suite": _job(
        "lj-" + "5" * 16,
        _S.RUNNING,
        claimed="2026-10-05T07:02:04Z",
        run_id="rlane-suite-omnibase-core-20565-e26864",
        ticket="OMN-20565",
    ),
}


def _spec(*criteria: ModelLabJobDoneCriterion) -> ModelLabJobSpec:
    return ModelLabJobSpec(
        job_id="lj-" + "c" * 16,
        kind=EnumLabJobKind.LANE,
        brief="do the thing",
        repo="OmniNode-ai/omnimarket",
        ref="a" * 40,
        engine="sonnet",
        time_box_min=30,
        done_criteria=criteria,
        parent_lane="parent",
        ticket="OMN-20604",
    )


class RecordingBus:
    def __init__(self) -> None:
        self.published: list[tuple[str, bytes | None, dict[str, Any]]] = []

    async def publish(
        self, topic: str, key: bytes | None, value: bytes, headers: Any = None
    ) -> object:
        self.published.append((topic, key, json.loads(value)))
        return None

    async def subscribe(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the check effect never subscribes through its handler")


class _Transaction:
    def __init__(self, log: list[str], readonly: bool) -> None:
        log.append(f"BEGIN readonly={readonly}")


class FakeConnection:
    """Records every statement; answers by matching the table the statement names."""

    def __init__(
        self, log: list[str], rows: Mapping[str, list[dict[str, Any]]]
    ) -> None:
        self.log = log
        self._rows = rows

    @asynccontextmanager
    async def transaction(self, *, readonly: bool = False) -> Any:
        _Transaction(self.log, readonly)
        yield

    async def fetch(self, sql: str, *args: object) -> list[dict[str, Any]]:
        self.log.append(sql.strip())
        for needle, rows in self._rows.items():
            if needle in sql:
                return rows
        raise AssertionError(f"no canned rows for: {sql[:80]}")


class FakePool:
    def __init__(self, conn: FakeConnection) -> None:
        self._conn = conn

    @asynccontextmanager
    async def acquire(self) -> Any:
        yield self._conn


class FakeAdapter:
    """The one attribute the store reads from ``AsyncpgAdapter``: its pool."""

    def __init__(self, conn: FakeConnection) -> None:
        self.pool = FakePool(conn)


def _state_row(job: ModelLabJobRow) -> dict[str, Any]:
    row = job.model_dump(mode="json")
    row["spec"] = job.spec.model_dump_json() if job.spec else None
    for key in ("entered_state_at", "claimed_at", "next_dispatch_at", "alert_sent_at"):
        value = getattr(job, key)
        row[key] = value
    return row
