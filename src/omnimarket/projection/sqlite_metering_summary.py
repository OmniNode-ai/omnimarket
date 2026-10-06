# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Local metering rows: refreshed at the end of a delegation, read as stored.

The SQLite database is one install's evidence store. ``tenant_id`` labels that
install's projection; the underlying legacy records do not carry tenant ids.
No arithmetic lives here: every figure comes from the metering node's fold.

Who writes the rows: the end of each mode 1 ``onex delegate``
(:func:`refresh_metering_after_terminal`, called by the local dispatch port once
the terminal row is durable). It issues the node's own fold request on the
node's own command topic, through a publisher; in mode 1 that publisher
delivers in-process to the node against this store. Who reads them:
``onex metering``, through
:mod:`omnimarket.projection.sqlite_metering_summary_reader`, which never folds
or writes.
"""

from __future__ import annotations

import fcntl
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from functools import cache
from pathlib import Path
from typing import Literal, Protocol

import yaml
from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_projection_metering_summary import (
    HandlerProjectionMeteringSummary,
    ModelMeteringSummaryFoldRequest,
    ModelMeteringSummaryRow,
)
from omnimarket.nodes.node_projection_metering_summary.baseline import resolve_baseline
from omnimarket.nodes.node_projection_metering_summary.handlers.handler_metering_summary_writer import (
    store_rows,
)
from omnimarket.pricing import BaselineModelSelection, resolve_baseline_model
from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter
from omnimarket.projection.sqlite_metering_reader import (
    read_metering_records,
)

_NODE_CONTRACT = (
    Path(__file__).resolve().parents[1]
    / "nodes"
    / "node_projection_metering_summary"
    / "contract.yaml"
)
# The fold's windows are half-open, [start, as_of): a run stamped exactly at
# as_of is outside its own row. The refresh closes the window one microsecond
# after the newest recorded run, the finest step an ISO timestamp carries.
_AFTER_NEWEST_RUN = timedelta(microseconds=1)
# How long the end-of-delegate refresh waits for another refresh of the same
# store before it gives up and says so. A refresh takes well under a second on
# a 25,000-run store; a holder past this is stuck, and the delegation must
# still return.
METERING_REFRESH_LOCK_TIMEOUT_SECONDS = 30.0
_LOCK_POLL_SECONDS = 0.05


def refresh_metering_summary(
    db_path: Path,
    tenant_id: str,
    baseline_model: str,
    now: datetime,
    *,
    days: frozenset[date] | None = None,
    include_fixtures: bool = False,
) -> tuple[ModelMeteringSummaryRow, ...]:
    """Refresh every recorded day and all-time, plus explicitly requested days."""
    records = read_metering_records(
        db_path=db_path, window_end=now, include_fixtures=include_fixtures
    )
    requested_days = days
    if days is not None:
        requested_days = days | {r.occurred_at.astimezone(UTC).date() for r in records}
    result = HandlerProjectionMeteringSummary().handle(
        ModelMeteringSummaryFoldRequest(
            tenant_id=tenant_id,
            records=records,
            baseline=resolve_baseline(baseline_model),
            baseline_model=baseline_model,
            as_of=now,
            days=requested_days,
        )
    )
    store_rows(SqliteDatabaseAdapter(db_path), result.rows)
    return result.rows


# -- the baseline the rows are folded and read under --------------------------


def resolve_metering_baseline_model() -> BaselineModelSelection:
    """The baseline a local run's saving is stated against (D3).

    The same resolution ``compute_baseline_savings`` makes for each run, so the
    rows the delegation refreshes and the row ``onex metering`` selects are
    keyed by one baseline string. Resolved on every call, never at import.
    """
    return resolve_baseline_model(overlay={}, store={})


def current_pricing_manifest_version(baseline_model: str) -> str | None:
    """The manifest version a row folded now would carry (None: unpriced)."""
    baseline = resolve_baseline(baseline_model)
    return baseline.pricing_manifest_version if baseline else None


# -- the node's own command, and how mode 1 delivers it ------------------------


@cache
def refresh_metering_summary_topic() -> str:
    """The metering node's command topic, as its contract declares it."""
    contract = yaml.safe_load(_NODE_CONTRACT.read_text(encoding="utf-8"))
    topics = contract["event_bus"]["subscribe_topics"]
    if len(topics) != 1:
        raise ValueError(
            f"{_NODE_CONTRACT} must subscribe exactly one refresh command topic, "
            f"got {topics!r}"
        )
    return str(topics[0])


class ProtocolMeteringRefreshPublisher(Protocol):
    """Delivers one fold request on the metering node's command topic."""

    def publish(self, topic: str, request: ModelMeteringSummaryFoldRequest) -> None:
        """Deliver ``request``; raise when it was not applied."""


class InProcessMeteringRefreshPublisher:
    """Mode 1: deliver the command to the node in this process, on this store.

    A laptop has no broker that should see this command: the local store backs
    no lane's projection, so a bus publish would fold this install's runs into
    a lane's table. The node runs here instead, exactly as its writer would.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path

    def publish(self, topic: str, request: ModelMeteringSummaryFoldRequest) -> None:
        expected = refresh_metering_summary_topic()
        if topic != expected:
            raise ValueError(
                f"metering refresh published on {topic!r}; the node subscribes "
                f"{expected!r}"
            )
        result = HandlerProjectionMeteringSummary().handle(request)
        store_rows(SqliteDatabaseAdapter(self._db_path), result.rows)


class ModelMeteringRefreshOutcome(BaseModel):
    """What one end-of-delegate refresh asked the node to fold."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: str
    baseline_model: str
    as_of: datetime
    # None: every recorded day, because the tenant's rows were folded under
    # another baseline or manifest version (or none exist yet).
    days: frozenset[date] | None
    whole_tenant_reason: str | None


@contextmanager
def metering_refresh_lock(db_path: Path) -> Iterator[None]:
    """Serialise refreshes of one store across processes.

    Two delegations finishing together would otherwise interleave read, fold
    and write, and the slower one could store a snapshot that misses the
    faster one's run over the row that holds it. With the lock, the refresh
    that writes last read every terminal row that was durable before it began.

    Raises :class:`TimeoutError` after ``METERING_REFRESH_LOCK_TIMEOUT_SECONDS``
    rather than waiting on a stuck holder for ever.
    """
    lock_path = db_path.with_name(db_path.name + ".metering-refresh.lock")
    deadline = time.monotonic() + METERING_REFRESH_LOCK_TIMEOUT_SECONDS
    with lock_path.open("a") as handle:
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"another metering refresh held {lock_path} for "
                        f"{METERING_REFRESH_LOCK_TIMEOUT_SECONDS:g}s"
                    ) from None
                time.sleep(_LOCK_POLL_SECONDS)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def refresh_metering_after_terminal(
    db_path: Path,
    *,
    tenant_id: str,
    terminal_at: datetime,
    publisher: ProtocolMeteringRefreshPublisher | None = None,
) -> ModelMeteringRefreshOutcome:
    """Fold the run's UTC day and the all row, once its terminal row is durable.

    ``as_of`` is one microsecond after the newest recorded run (never the wall
    clock), so the run is inside its own row, a second delivery of the same
    terminal changes no byte, and a late refresh for an earlier run folds the
    same snapshot as the latest one instead of an older one.

    When the tenant's all row under the resolved baseline is missing or was
    priced against another manifest version, the request covers the whole
    tenant (``days=None``): a baseline or pricing change recomputes every row
    (ruling D-A), and a first refresh backfills every recorded day.

    Only real runs are folded: rows the dev seed wrote are never in a stored
    figure (OMN-19970).
    """
    if terminal_at.utcoffset() is None:
        raise ValueError("terminal_at must be timezone aware")
    sink = publisher or InProcessMeteringRefreshPublisher(db_path)
    with metering_refresh_lock(db_path):
        model = resolve_metering_baseline_model().model
        baseline = resolve_baseline(model)
        version = current_pricing_manifest_version(model)
        records = read_metering_records(db_path=db_path)
        newest = max([terminal_at, *(r.occurred_at for r in records)])
        as_of = newest.astimezone(UTC) + _AFTER_NEWEST_RUN
        stored = read_summary_row(db_path, tenant_id, "all", "", model)
        reason: str | None = None
        if stored is None:
            reason = f"no stored all row for baseline {model}"
        elif stored.pricing_manifest_version != version:
            reason = (
                f"rows priced against manifest {stored.pricing_manifest_version}, "
                f"current {version}"
            )
        days = (
            None
            if reason is not None
            else frozenset({terminal_at.astimezone(UTC).date()})
        )
        sink.publish(
            refresh_metering_summary_topic(),
            ModelMeteringSummaryFoldRequest(
                tenant_id=tenant_id,
                records=records,
                baseline=baseline,
                baseline_model=model,
                as_of=as_of,
                days=days,
            ),
        )
    return ModelMeteringRefreshOutcome(
        tenant_id=tenant_id,
        baseline_model=model,
        as_of=as_of,
        days=days,
        whole_tenant_reason=reason,
    )


def read_summary_row(
    db_path: Path,
    tenant_id: str,
    window_kind: Literal["day", "all"],
    window_start: str,
    baseline_model: str,
) -> ModelMeteringSummaryRow | None:
    """Read one complete snapshot. A missing file is not created by lookup."""
    if not db_path.exists():
        return None
    rows = SqliteDatabaseAdapter(db_path).query(
        "metering_summary",
        {
            "tenant_id": tenant_id,
            "window_kind": window_kind,
            "window_start": window_start,
            "baseline_model": baseline_model,
        },
    )
    return ModelMeteringSummaryRow.model_validate(rows[0]) if rows else None
