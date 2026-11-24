# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The live profile, the binding site: every seam is bound to its live source, and none is left unbound.

Bound live: the state store (the ``floor_fill_state`` table of omnibase_infra, migration 113), the floor record (URL)
and plan texts (files), the ledger (the rolling ledger file), the lane process runner (the posix runner of
``node_lane_process_effect``), the continuation briefs (directories) and the wall clock; and the four seams that were
once left to injection: the pool (the lane host pool snapshot), the tickets (the floor-task ticket nodes over the Linear
store), and the brake and stop signal (the andon stop gate over the stop queue; the brake is also engaged when fewer
hosts than ``brake_min_admitting_hosts`` admit a lane, read from the pool snapshot). A source that cannot be read answers
``UNKNOWN <source>-unread:<cause>`` and the pass starts nothing; no seam is ever bound to an "unbound" stand-in.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import ClassVar

from omnibase_spi.contracts.services import (
    ModelLaneHostPoolRequest,
    ModelLaneHostPoolSnapshot,
)
from omnibase_spi.protocols.lane import (
    ProtocolFloorTicketStore,
    ProtocolLaneHostPool,
    ProtocolLaneProcessRunner,
)
from pydantic import SecretStr

from omnimarket.nodes.node_floor_fill_candidates_compute.handlers.handler_floor_fill_candidates import (
    HandlerFloorFillCandidates,
)
from omnimarket.nodes.node_floor_fill_lines_compute.handlers import (
    HandlerFloorFillLines,
)
from omnimarket.nodes.node_floor_fill_orchestrator.handlers.adapter_floor_facts_live import (
    LiveFloorFacts,
    fetch_url_text,
    read_text_file,
)
from omnimarket.nodes.node_floor_fill_orchestrator.handlers.adapter_floor_seams_live import (
    LiveFloorCapacityBrake,
    LiveFloorPool,
    LiveFloorStopSignals,
    LiveFloorTickets,
    LiveSeamSources,
    default_stop_facts,
    engaged_by_any,
    unconfigured_stop_facts,
)
from omnimarket.nodes.node_floor_fill_orchestrator.handlers.adapter_ledger_file import (
    FileWorkLedgerStore,
)
from omnimarket.nodes.node_floor_fill_orchestrator.handlers.handler_floor_fill_command import (
    HandlerFloorFillCommand,
)
from omnimarket.nodes.node_floor_fill_orchestrator.models import (
    ModelFloorFillConfig,
    ModelFloorFillStoreSettings,
)
from omnimarket.nodes.node_floor_fill_orchestrator.models.model_floor_fill_live_settings import (
    LIVE_OVERLAY_SECTION,
    ModelFloorFillLiveSettings,
)
from omnimarket.nodes.node_floor_fill_orchestrator.orchestration.adapters import (
    DirectoryFloorBriefs,
    LedgerRowsFloorLedger,
)
from omnimarket.nodes.node_floor_fill_orchestrator.orchestration.core import (
    FloorFillOrchestrator,
)
from omnimarket.nodes.node_floor_fill_orchestrator.orchestration.ports import (
    FloorFillUnreadableError,
    ProtocolFloorClock,
    ProtocolFloorPool,
    ProtocolFloorStateStore,
    ProtocolFloorTickets,
)
from omnimarket.nodes.node_floor_fill_orchestrator.orchestration.system_clock import (
    SystemFloorClock,
)
from omnimarket.nodes.node_floor_fill_reducer.handlers import HandlerFloorFillReducer
from omnimarket.nodes.node_lane_host_pool_snapshot_effect.handlers.adapter_snapshot_file import (
    StampedSnapshotFileLaneHostPool,
)
from omnimarket.nodes.node_lane_process_effect.handlers.handler_posix_process import (
    HandlerPosixProcess,
)


class _UnconfiguredLanePool:
    """A lane host pool whose overlay keys are absent: every read fails naming them, so the pool seam is unread."""

    synchronous_execution: ClassVar[bool] = True

    def __init__(self, keys: Sequence[str]) -> None:
        self._keys = ",".join(keys)

    def read_pool(self, request: ModelLaneHostPoolRequest) -> ModelLaneHostPoolSnapshot:
        raise FloorFillUnreadableError(f"unconfigured:{self._keys}")


def _snapshot_lane_pool(
    settings: ModelFloorFillLiveSettings, now: Callable[[], datetime]
) -> ProtocolLaneHostPool:
    """The stamped lane host pool snapshot the pool and the brake both read; a missing, stale or empty one is unread."""
    path = settings.pool_snapshot_path
    suffix = settings.pool_record_suffix
    max_age = settings.pool_snapshot_max_age_seconds
    if (
        path is None
        or suffix is None
        or max_age is None
        or settings.pool_engine is None
    ):
        absent = [
            f"{LIVE_OVERLAY_SECTION}.{name}"
            for name in (
                "pool_snapshot_path",
                "pool_record_suffix",
                "pool_snapshot_max_age_seconds",
                "pool_engine",
            )
            if getattr(settings, name) is None
        ]
        return _UnconfiguredLanePool(absent)
    return StampedSnapshotFileLaneHostPool(Path(path), suffix, max_age, now)


def _linear_ticket_store(
    lane: str, settings: ModelFloorFillLiveSettings
) -> Callable[[], ProtocolFloorTicketStore]:
    """The ticket store over Linear, built on first use: a missing credential is an unread tickets source then."""

    def build_store() -> ProtocolFloorTicketStore:
        from omnimarket.handlers.floor_task_ticket_linear_store import (
            build_linear_store,
        )

        timeout = settings.mint_timeout_seconds
        if timeout is None:
            raise FloorFillUnreadableError(
                f"unconfigured:{LIVE_OVERLAY_SECTION}.mint_timeout_seconds"
            )
        return build_linear_store(lane, timeout_seconds=timeout)

    return build_store


def build_live_handler_factory(
    settings: ModelFloorFillLiveSettings,
    *,
    dsn: str,
    store: ProtocolFloorStateStore | None = None,
    fetch_text: Callable[[str, float], str] = fetch_url_text,
    read_file: Callable[[str], str] = read_text_file,
    pool: ProtocolFloorPool | None = None,
    tickets: ProtocolFloorTickets | None = None,
    brake: Callable[[], bool] | None = None,
    stop: Callable[[str], str | None] | None = None,
    runner: ProtocolLaneProcessRunner | None = None,
    clock: ProtocolFloorClock | None = None,
    sources: LiveSeamSources | None = None,
) -> Callable[[ModelFloorFillConfig], HandlerFloorFillCommand]:
    """The factory ``run_entry`` takes: given the validated config it wires the live seams.

    ``store``, ``runner``, ``clock`` and ``sources`` are for tests and other deployments; without them the table-backed
    store, the posix runner, the wall clock and the default reads (Linear ticket store, exception-queue stop reads)
    are built from the settings and ``dsn``. ``pool``, ``tickets``, ``brake`` and ``stop`` override one live seam each;
    left ``None`` the seam is bound to its live reader, so the factory has no unbound seam.
    """
    live_sources = sources if sources is not None else LiveSeamSources()
    now = clock if clock is not None else SystemFloorClock()

    def now_utc() -> datetime:
        return now.now()

    def build(config: ModelFloorFillConfig) -> HandlerFloorFillCommand:
        bound_store = store
        if bound_store is None:
            from omnimarket.handlers.floor_fill_state_store_postgres import (
                PostgresFloorStateStore,
            )

            bound_store = PostgresFloorStateStore(
                ModelFloorFillStoreSettings(
                    dsn=SecretStr(dsn),
                    schema_name=settings.store_schema_name,
                    table_name=settings.store_table_name,
                    connect_timeout_seconds=settings.store_connect_timeout_seconds,
                )
            )
        process = (
            runner
            if runner is not None
            else HandlerPosixProcess(settings.process_output_dir)
        )
        ticket_store = live_sources.ticket_store
        if ticket_store is None:
            ticket_store = _linear_ticket_store(config.scope, settings)
        stop_facts = live_sources.stop_facts
        if stop_facts is None:
            if settings.andon_state_dir is None:
                stop_facts = unconfigured_stop_facts(
                    f"{LIVE_OVERLAY_SECTION}.andon_state_dir"
                )
            else:
                stop_facts = default_stop_facts(
                    Path(settings.andon_state_dir), now_utc, settings.stop_policy()
                )
        signals = LiveFloorStopSignals(stop_facts)
        lane_pool = _snapshot_lane_pool(settings, now_utc)
        engine = settings.pool_engine or ""
        capacity_brake = LiveFloorCapacityBrake(
            lane_pool, engine, settings.brake_min_admitting_hosts
        )
        orchestrator = FloorFillOrchestrator(
            config=config,
            candidates=HandlerFloorFillCandidates(),
            reducer=HandlerFloorFillReducer(),
            lines=HandlerFloorFillLines(),
            store=bound_store,
            facts=LiveFloorFacts(
                fetch_text=fetch_text,
                read_file=read_file,
                timeout_seconds=settings.floor_fetch_timeout_seconds,
                plan_file_pattern=settings.plan_file_pattern,
                brake=brake
                if brake is not None
                else engaged_by_any(capacity_brake, signals.brake),
                stop=stop if stop is not None else signals.stop,
            ),
            pool=pool if pool is not None else LiveFloorPool(lane_pool, engine),
            ledger=LedgerRowsFloorLedger(FileWorkLedgerStore(settings.ledger_path)),
            tickets=tickets
            if tickets is not None
            else LiveFloorTickets(
                ticket_store, mint_timeout_seconds=settings.mint_timeout_seconds
            ),
            runner=process,
            briefs=DirectoryFloorBriefs(
                config.stopped_reports_root, config.stopped_brief_directory
            ),
            clock=now,
        )
        return HandlerFloorFillCommand(
            config=config, orchestrator=orchestrator, runner=process
        )

    return build


__all__: list[str] = ["build_live_handler_factory"]
