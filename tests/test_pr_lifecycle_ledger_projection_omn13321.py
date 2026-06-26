# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-13321 / F5: per-iteration durable PR ledger projection.

The state reducer must materialize one user-readable ledger row per PR on
**every** sweep iteration (not only at sweep end), keyed by
``(sweep_id, repo, pr_number, iteration)``. The DoD probe is:

    select count(*) from pr_lifecycle_ledger_entries where sweep_id = '<id>';

which must be >= the open-PR count for that sweep, and two consecutive
iterations must both produce rows.

This test exercises:
  * the pure row-builder (``build_ledger_rows``) -- exact ticket fields;
  * the reducer ``handle()`` orchestrator path writing rows through the injected
    projection database, one row per PR per iteration;
  * two consecutive iterations both producing distinct durable rows;
  * row count >= PR count for the sweep;
  * fail-fast when a classified PR has no matching intent;
  * contract.yaml declaring the topic/table (not hardcoded only in code).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
import yaml

from omnimarket.nodes.node_pr_lifecycle_orchestrator.protocols.protocol_sub_handlers import (
    EnumPrCategory,
    EnumReducerIntent,
    ReducerIntent,
    ReducerResult,
    TriageRecord,
)
from omnimarket.nodes.node_pr_lifecycle_state_reducer.handlers.handler_pr_lifecycle_state_reducer import (
    HandlerPrLifecycleStateReducer,
)
from omnimarket.projection.pr_ledger_projection import (
    PR_LEDGER_PROJECTION_CONFLICT_KEY,
    PR_LEDGER_PROJECTION_FRESHNESS_SLA_SECONDS,
    PR_LEDGER_PROJECTION_TABLE,
    PR_LEDGER_PROJECTION_TOPIC,
    EnumPrLedgerAction,
    EnumPrLedgerFinalState,
    ModelPrLedgerProjectionRow,
    build_ledger_rows,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

_CONTRACT = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_pr_lifecycle_state_reducer"
    / "contract.yaml"
)

_FOUND_AT = datetime(2026, 6, 26, 12, 0, 0, tzinfo=UTC)


def _green(pr_number: int, repo: str = "OmniNode-ai/omnimarket") -> TriageRecord:
    return TriageRecord(pr_number=pr_number, repo=repo, category=EnumPrCategory.GREEN)


def _red(pr_number: int, repo: str = "OmniNode-ai/omnibase_core") -> TriageRecord:
    return TriageRecord(
        pr_number=pr_number,
        repo=repo,
        category=EnumPrCategory.RED,
        block_reason="required check 'verify' failed",
        failed_check_names=("verify",),
    )


# ---------------------------------------------------------------------------
# Pure row-builder
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBuildLedgerRows:
    def test_one_row_per_pr_with_exact_ticket_fields(self) -> None:
        classified = (_green(1), _red(2))
        intents = (
            ReducerIntent(
                pr_number=1,
                repo="OmniNode-ai/omnimarket",
                intent=EnumReducerIntent.MERGE,
            ),
            ReducerIntent(
                pr_number=2,
                repo="OmniNode-ai/omnibase_core",
                intent=EnumReducerIntent.FIX,
                reason="required check 'verify' failed",
            ),
        )
        rows = build_ledger_rows(
            sweep_id="sweep-A",
            iteration=0,
            found_at=_FOUND_AT,
            classified=classified,
            intents=intents,
        )
        assert len(rows) == 2
        # ordered (repo, pr_number): omnibase_core#2 before omnimarket#1
        core_row, market_row = rows
        assert isinstance(core_row, ModelPrLedgerProjectionRow)
        assert core_row.repo == "OmniNode-ai/omnibase_core"
        assert core_row.pr_number == 2
        assert core_row.sweep_id == "sweep-A"
        assert core_row.iteration == 0
        assert core_row.found_at == _FOUND_AT
        assert core_row.initial_state == "red"
        assert core_row.action_taken is EnumPrLedgerAction.FIX
        assert core_row.evidence == "required check 'verify' failed"
        assert core_row.final_state is EnumPrLedgerFinalState.FIX_DISPATCHED
        # next_check_at = found_at + freshness SLA
        delta = (core_row.next_check_at - core_row.found_at).total_seconds()
        assert delta == PR_LEDGER_PROJECTION_FRESHNESS_SLA_SECONDS

        assert market_row.action_taken is EnumPrLedgerAction.MERGE
        assert market_row.final_state is EnumPrLedgerFinalState.MERGE_REQUESTED
        assert market_row.initial_state == "green"

    def test_evidence_falls_back_to_block_reason(self) -> None:
        classified = (_red(5),)
        intents = (
            ReducerIntent(
                pr_number=5,
                repo="OmniNode-ai/omnibase_core",
                intent=EnumReducerIntent.FIX,
            ),
        )
        (row,) = build_ledger_rows(
            sweep_id="s",
            iteration=1,
            found_at=_FOUND_AT,
            classified=classified,
            intents=intents,
        )
        assert row.evidence == "required check 'verify' failed"

    def test_missing_intent_for_classified_pr_raises(self) -> None:
        with pytest.raises(ValueError, match="no reducer intent"):
            build_ledger_rows(
                sweep_id="s",
                iteration=0,
                found_at=_FOUND_AT,
                classified=(_green(1),),
                intents=(),
            )

    def test_serialized_row_carries_ticket_columns(self) -> None:
        (row,) = build_ledger_rows(
            sweep_id="s",
            iteration=0,
            found_at=_FOUND_AT,
            classified=(_green(1),),
            intents=(
                ReducerIntent(
                    pr_number=1,
                    repo="OmniNode-ai/omnimarket",
                    intent=EnumReducerIntent.MERGE,
                ),
            ),
        )
        serialized = row.to_row()
        for col in (
            "sweep_id",
            "iteration",
            "found_at",
            "repo",
            "pr_number",
            "initial_state",
            "action_taken",
            "evidence",
            "final_state",
            "next_check_at",
        ):
            assert col in serialized


# ---------------------------------------------------------------------------
# Reducer handle() per-iteration emission
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestReducerEmitsLedgerEveryIteration:
    def test_handle_writes_one_row_per_pr_when_db_injected(self) -> None:
        handler = HandlerPrLifecycleStateReducer()
        db = InmemoryDatabaseAdapter()
        classified = (_green(1), _red(2), _green(3))

        result = asyncio.run(
            handler.handle(
                correlation_id=uuid4(),
                classified=classified,
                dry_run=False,
                inventory_only=False,
                fix_only=False,
                merge_only=False,
                projection_db=db,
                sweep_id="sweep-xyz",
                iteration=0,
                found_at=_FOUND_AT,
            )
        )
        assert isinstance(result, ReducerResult)

        rows = db.query(PR_LEDGER_PROJECTION_TABLE, {"sweep_id": "sweep-xyz"})
        # DoD: row count >= open-PR count for the sweep.
        assert len(rows) >= len(classified)
        assert len(rows) == len(classified)

    def test_two_consecutive_iterations_both_produce_rows(self) -> None:
        handler = HandlerPrLifecycleStateReducer()
        db = InmemoryDatabaseAdapter()
        classified = (_green(1), _red(2))

        for iteration in (0, 1):
            asyncio.run(
                handler.handle(
                    correlation_id=uuid4(),
                    classified=classified,
                    dry_run=False,
                    inventory_only=False,
                    fix_only=False,
                    merge_only=False,
                    projection_db=db,
                    sweep_id="sweep-2it",
                    iteration=iteration,
                    found_at=_FOUND_AT,
                )
            )

        rows = db.query(PR_LEDGER_PROJECTION_TABLE, {"sweep_id": "sweep-2it"})
        # Two iterations x two PRs = four distinct rows (iteration in conflict key).
        assert len(rows) == 4
        iterations = sorted({int(r["iteration"]) for r in rows})  # type: ignore[call-overload]
        assert iterations == [0, 1]
        # both iterations produced a row for each PR
        for it in (0, 1):
            it_rows = [r for r in rows if r["iteration"] == it]
            assert len(it_rows) == 2

    def test_no_db_no_sweep_id_keeps_pure_fsm_behavior(self) -> None:
        """Without projection_db + sweep_id the reducer is the pure classifier."""
        handler = HandlerPrLifecycleStateReducer()
        result = asyncio.run(
            handler.handle(
                correlation_id=uuid4(),
                classified=(_green(1),),
                dry_run=False,
                inventory_only=False,
                fix_only=False,
                merge_only=False,
            )
        )
        assert isinstance(result, ReducerResult)
        assert result.merge_count == 1

    def test_db_without_sweep_id_fails_fast(self) -> None:
        handler = HandlerPrLifecycleStateReducer()
        db = InmemoryDatabaseAdapter()
        with pytest.raises(ValueError, match="requires BOTH projection_db"):
            asyncio.run(
                handler.handle(
                    correlation_id=uuid4(),
                    classified=(_green(1),),
                    dry_run=False,
                    inventory_only=False,
                    fix_only=False,
                    merge_only=False,
                    projection_db=db,
                    iteration=0,
                    found_at=_FOUND_AT,
                )
            )


# ---------------------------------------------------------------------------
# Contract declares topic + table (not hardcoded only in code).
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestContractDeclaresProjection:
    def test_contract_projection_api_declares_topic_and_table(self) -> None:
        data = yaml.safe_load(_CONTRACT.read_text())
        proj = data["projection_api"]
        assert proj["expose"] is True
        exposures = proj["exposures"]
        match = [
            e
            for e in exposures
            if e["topic"] == PR_LEDGER_PROJECTION_TOPIC
            and e["table"] == PR_LEDGER_PROJECTION_TABLE
        ]
        assert match, "contract must declare the ledger projection topic+table"
        cols = match[0]["columns"]
        for col in (
            "sweep_id",
            "iteration",
            "found_at",
            "repo",
            "pr_number",
            "initial_state",
            "action_taken",
            "evidence",
            "final_state",
            "next_check_at",
        ):
            assert col in cols

    def test_contract_db_io_declares_table_with_migration(self) -> None:
        data = yaml.safe_load(_CONTRACT.read_text())
        tables = data["db_io"]["db_tables"]
        match = [t for t in tables if t["name"] == PR_LEDGER_PROJECTION_TABLE]
        assert match, "db_io must declare the ledger table"
        assert match[0]["database"] == "omnidash_analytics"
        assert match[0]["migration"]

    def test_conflict_key_matches_constant(self) -> None:
        assert PR_LEDGER_PROJECTION_CONFLICT_KEY == "sweep_id,repo,pr_number,iteration"


# ---------------------------------------------------------------------------
# Orchestrator-path integration: the REAL reducer, driven by the orchestrator,
# materializes ledger rows per iteration into the injected projection database.
# This is the DoD surface -- one row per PR each iteration, count >= PR count.
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestOrchestratorEmitsLedgerPerIteration:
    async def test_orchestrator_drives_real_reducer_into_projection_db(self) -> None:
        from typing import cast

        from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
        from omnibase_core.protocols.event_bus.protocol_event_bus_publisher import (
            ProtocolEventBusPublisher,
        )

        from omnimarket.nodes.node_pr_lifecycle_orchestrator.handlers.handler_pr_lifecycle_orchestrator import (
            ModelPrLifecycleStartCommand,
        )
        from omnimarket.nodes.node_pr_lifecycle_orchestrator.protocols.protocol_sub_handlers import (
            PrRecord,
        )

        # Reuse the golden-chain test harness (real reducer, stubbed gh CLI).
        from tests.test_golden_chain_pr_lifecycle_orchestrator import (
            MockInventory,
            MockTriage,
            _TestOrchestrator,
        )

        prs = (
            PrRecord(
                pr_number=101, repo="OmniNode-ai/omnimarket", checks_status="success"
            ),
            PrRecord(
                pr_number=202, repo="OmniNode-ai/omnibase_core", checks_status="failure"
            ),
            PrRecord(
                pr_number=303, repo="OmniNode-ai/omnidash", checks_status="success"
            ),
        )
        classified = (
            TriageRecord(
                pr_number=101,
                repo="OmniNode-ai/omnimarket",
                category=EnumPrCategory.GREEN,
            ),
            TriageRecord(
                pr_number=202,
                repo="OmniNode-ai/omnibase_core",
                category=EnumPrCategory.RED,
                block_reason="required check failed",
            ),
            TriageRecord(
                pr_number=303,
                repo="OmniNode-ai/omnidash",
                category=EnumPrCategory.GREEN,
            ),
        )
        db = InmemoryDatabaseAdapter()
        raw_bus = EventBusInmemory()
        await raw_bus.start()
        bus = cast(ProtocolEventBusPublisher, raw_bus)

        inv = MockInventory(prs=prs)
        orch = _TestOrchestrator(
            _mock_inventory_prs=prs,
            inventory=inv,
            triage=MockTriage(classified=classified),
            reducer=HandlerPrLifecycleStateReducer(),
            event_bus=bus,
            projection_db=db,
        )

        sweep_id = "20260626-orch-it"
        # Two consecutive orchestrator passes sharing the same run_id (sweep_id).
        for _ in range(2):
            await orch.handle(
                ModelPrLifecycleStartCommand(
                    correlation_id=uuid4(),
                    run_id=sweep_id,
                    dry_run=True,  # dry_run still classifies + emits ledger rows
                )
            )

        rows = db.query(PR_LEDGER_PROJECTION_TABLE, {"sweep_id": sweep_id})
        # DoD: count >= open-PR count for the sweep.
        assert len(rows) >= len(prs)
        iterations = sorted({int(r["iteration"]) for r in rows})  # type: ignore[call-overload]
        assert iterations == [0, 1], f"expected two iterations, got {iterations}"
        # Each iteration produced a row per PR.
        for it in (0, 1):
            it_rows = [r for r in rows if r["iteration"] == it]
            assert len(it_rows) == len(prs)
