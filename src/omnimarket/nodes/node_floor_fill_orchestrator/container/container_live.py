# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The live profile, the binding site: the seams that have a live source are bound to it, the rest fail closed.

Bound live: the state store (the ``floor_fill_state`` table of omnibase_infra, migration 113), the floor record (URL)
and plan texts (files), the ledger (the rolling ledger file), the lane process runner (the posix runner of
``node_lane_process_effect``), the continuation briefs (directories) and the wall clock. The pool, tickets, brake and stop-signal seams have no live source
of their own (no placement reader, the floor-task ticket nodes off the dev branch, no open-count brake, no stop
signal): they are bound by injection, and a missing one (left ``None``) refuses by name, making the pass answer
``UNKNOWN <source>-unread:unbound:<seam>`` and start nothing.
"""

from __future__ import annotations

from collections.abc import Callable

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
    ProtocolFloorPool,
    ProtocolFloorStateStore,
    ProtocolFloorTickets,
)
from omnimarket.nodes.node_floor_fill_orchestrator.orchestration.system_clock import (
    SystemFloorClock,
)
from omnimarket.nodes.node_floor_fill_orchestrator.orchestration.unbound_ports import (
    UnboundFloorPool,
    UnboundFloorTickets,
)
from omnimarket.nodes.node_floor_fill_reducer.handlers import HandlerFloorFillReducer
from omnimarket.nodes.node_lane_process_effect.handlers.handler_posix_process import (
    HandlerPosixProcess,
)


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
) -> Callable[[ModelFloorFillConfig], HandlerFloorFillCommand]:
    """The factory ``run_entry`` takes: given the validated config it wires the live seams.

    ``store`` is for tests; without one the table-backed store is built from the settings and ``dsn``.
    ``pool``, ``tickets``, ``brake`` and ``stop`` are bound by injection; a missing one (left ``None``)
    refuses by name, so the pass answers ``UNKNOWN <source>-unread:unbound:<seam>`` and starts nothing.
    """

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
        process = HandlerPosixProcess(settings.process_output_dir)
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
                brake=brake,
                stop=stop,
            ),
            pool=pool if pool is not None else UnboundFloorPool(),
            ledger=LedgerRowsFloorLedger(FileWorkLedgerStore(settings.ledger_path)),
            tickets=tickets if tickets is not None else UnboundFloorTickets(),
            runner=process,
            briefs=DirectoryFloorBriefs(
                config.stopped_reports_root, config.stopped_brief_directory
            ),
            clock=SystemFloorClock(),
        )
        return HandlerFloorFillCommand(
            config=config, orchestrator=orchestrator, runner=process
        )

    return build


__all__: list[str] = ["build_live_handler_factory"]
