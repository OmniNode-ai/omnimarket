# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerLaneLivenessGather: the scheduled producer of the liveness command.

The I/O half of the first hook-ledger reader. It performs read-only SELECTs
against ``public.hook_events`` and the work-ledger projection, assembles a
``ModelLaneLivenessRequest`` and returns it; every verdict is decided by
``HandlerLaneLiveness`` downstream and none is decided here.

THE LANE IS THE KEY, AND ``session_id`` IS NOT. One ``session_id`` covered
79,343 of the table's 83,067 rows, because it delimits a RESUMED session. The
queries group by ``payload->>'lane'`` only.
"""

from __future__ import annotations

import datetime as dt
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol

import yaml
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.model_lane_liveness import (
    ModelLaneLivenessRequest,
    ModelLaneObservation,
)

_CONTRACT = Path(__file__).resolve().parents[1] / "contract.yaml"
_COMPUTE_CONTRACT = (
    Path(__file__).resolve().parents[2] / "node_lane_liveness_compute" / "contract.yaml"
)


class ModelLaneLivenessGatherConfig(BaseModel):
    """The ``config.lane_liveness_gather`` block of the contract."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_interval_seconds: int = Field(..., gt=0)
    window_minutes: int = Field(..., gt=0)
    silence_threshold_seconds: int = Field(..., gt=0)
    relay_silence_threshold_seconds: int = Field(..., gt=0)
    hook_events_dsn_env: str
    ledger_dsn_env: str
    ledger_id: str


class ModelLaneWindowRead(BaseModel):
    """What the two surfaces held for one window."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lane_rows: tuple[dict[str, Any], ...] = ()
    relay_last_event_at: dt.datetime | None = None
    relay_event_count: int = Field(default=0, ge=0)
    attributed_event_count: int = Field(default=0, ge=0)
    claims: dict[str, dt.datetime] = Field(default_factory=dict)
    terminals: dict[str, dt.datetime] = Field(default_factory=dict)


class ProtocolLaneWindowReader(Protocol):
    def read_window(
        self, window_start: dt.datetime, window_end: dt.datetime
    ) -> ModelLaneWindowRead: ...


@lru_cache(maxsize=1)
def gather_config() -> ModelLaneLivenessGatherConfig:
    data = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    return ModelLaneLivenessGatherConfig.model_validate(
        data["config"]["lane_liveness_gather"]
    )


def activity_event_types(contract_path: Path | None = None) -> tuple[str, ...]:
    """The lane-activity classes the compute node's contract declares.

    Raises rather than defaulting: a reader that queries the wrong classes
    returns an empty result, and an empty result here reads as a quiet fleet.
    """
    contract = yaml.safe_load(
        (contract_path or _COMPUTE_CONTRACT).read_text(encoding="utf-8")
    )
    declared = contract["lane_activity_event_types"]
    if not declared:
        raise ValueError(
            "lane_activity_event_types is empty in node_lane_liveness_compute's "
            "contract; refusing to query with no event classes, which would return a false zero"
        )
    return tuple(str(entry) for entry in declared)


class PostgresLaneWindowReader:
    """Read-only reads of ``public.hook_events`` and the work-ledger projection."""

    def __init__(self, config: ModelLaneLivenessGatherConfig) -> None:
        self._cfg = config

    def _connect(self, dsn_env: str) -> Any:
        dsn = os.environ.get(dsn_env, "")
        if not dsn:
            raise RuntimeError(
                f"{dsn_env} is unset; refusing to read from a guessed database, "
                "which would report a healthy fleet from an empty table"
            )
        from omnimarket.projection.postgres_read_database import connect_read_only

        return connect_read_only(dsn)

    def read_window(
        self, window_start: dt.datetime, window_end: dt.datetime
    ) -> ModelLaneWindowRead:
        hook = self._connect(self._cfg.hook_events_dsn_env)
        try:
            with hook.cursor() as cur:
                cur.execute(
                    """
                    SELECT payload->>'lane', MAX(occurred_at), COUNT(*)
                      FROM public.hook_events
                     WHERE occurred_at >= %s AND occurred_at < %s
                       AND event_type = ANY(%s)
                       AND COALESCE(payload->>'lane', '') <> ''
                     GROUP BY 1
                    """,
                    (window_start, window_end, list(activity_event_types())),
                )
                lane_rows = tuple(
                    {"lane": lane, "last_event_at": last, "event_count": int(n)}
                    for lane, last, n in cur.fetchall()
                )
                cur.execute(
                    """
                    SELECT MAX(occurred_at), COUNT(*),
                           COUNT(*) FILTER (WHERE COALESCE(payload->>'lane', '') <> '')
                      FROM public.hook_events
                     WHERE occurred_at >= %s AND occurred_at < %s
                    """,
                    (window_start, window_end),
                )
                relay_last, relay_count, attributed = cur.fetchone()
        finally:
            hook.close()
        ledger = self._connect(self._cfg.ledger_dsn_env)
        try:
            with ledger.cursor() as cur:
                cur.execute(
                    """
                    SELECT row_type, row_lane, MAX(row_ts)
                      FROM omninode_internal.work_ledger_rows
                     WHERE ledger_id = %s AND row_ts <= %s
                       AND row_type IN ('CLAIM', 'TERMINAL')
                       AND COALESCE(row_lane, '') <> ''
                     GROUP BY row_type, row_lane
                    """,
                    (self._cfg.ledger_id, window_end),
                )
                rows = cur.fetchall()
        finally:
            ledger.close()
        return ModelLaneWindowRead(
            lane_rows=lane_rows,
            relay_last_event_at=relay_last,
            relay_event_count=int(relay_count),
            attributed_event_count=int(attributed),
            claims={lane: ts for kind, lane, ts in rows if kind == "CLAIM"},
            terminals={lane: ts for kind, lane, ts in rows if kind == "TERMINAL"},
        )


def build_request(
    *,
    window_start: dt.datetime,
    window_end: dt.datetime,
    read: ModelLaneWindowRead,
    config: ModelLaneLivenessGatherConfig,
) -> ModelLaneLivenessRequest:
    """Assemble the compute node's input from both surfaces.

    The observation set is the UNION of lanes seen on the wire and lanes named
    by a ledger row: a lane only the ledger knows is exactly the case drop
    detection exists for.
    """
    activity = {
        str(row["lane"]): (row["last_event_at"], int(row["event_count"]))
        for row in read.lane_rows
        if row.get("lane")
    }
    lanes = sorted(set(activity) | set(read.claims) | set(read.terminals))
    return ModelLaneLivenessRequest(
        window_start=window_start,
        window_end=window_end,
        observations=tuple(
            ModelLaneObservation(
                lane=lane,
                claimed_at=read.claims.get(lane),
                terminal_at=read.terminals.get(lane),
                last_hook_event_at=activity.get(lane, (None, 0))[0],
                hook_event_count=activity.get(lane, (None, 0))[1],
            )
            for lane in lanes
        ),
        relay_last_event_at=read.relay_last_event_at,
        relay_event_count=read.relay_event_count,
        # False unless at least one event in the window carried a lane: a window
        # of pre-emitter rows must not be read by lane at any threshold.
        lane_attribution_available=read.attributed_event_count > 0,
        silence_threshold_seconds=config.silence_threshold_seconds,
        relay_silence_threshold_seconds=config.relay_silence_threshold_seconds,
    )


class HandlerLaneLivenessGather:
    """EFFECT handler: tick in, gathered liveness request out once per interval."""

    def __init__(
        self,
        *,
        reader: ProtocolLaneWindowReader | None = None,
        config: ModelLaneLivenessGatherConfig | None = None,
    ) -> None:
        self._cfg = config or gather_config()
        # The default reader opens no connection until the first read, so the
        # runtime can construct this handler at wiring time on a lane that has
        # not bound the DSNs yet.
        self._reader: ProtocolLaneWindowReader = reader or PostgresLaneWindowReader(
            self._cfg
        )
        self._last_run: dt.datetime | None = None

    def handle(self, request: ModelRuntimeTick) -> ModelLaneLivenessRequest | None:
        now = request.now.astimezone(dt.UTC)
        interval = dt.timedelta(seconds=self._cfg.run_interval_seconds)
        if self._last_run is not None and now - self._last_run < interval:
            return None
        window_start = now - dt.timedelta(minutes=self._cfg.window_minutes)
        read = self._reader.read_window(window_start, now)
        # Recorded only after the read succeeded, so a failed read is retried on
        # the next tick rather than waiting out the interval.
        self._last_run = now
        return build_request(
            window_start=window_start,
            window_end=now,
            read=read,
            config=self._cfg,
        )


__all__: list[str] = [
    "HandlerLaneLivenessGather",
    "ModelLaneLivenessGatherConfig",
    "ModelLaneWindowRead",
    "PostgresLaneWindowReader",
    "build_request",
    "gather_config",
]
