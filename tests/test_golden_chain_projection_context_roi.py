# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain tests for node_projection_context_roi (OMN-12955).

Proves the closure loop: context-ROI runner terminal event payload ->
context_roi_scores rows -> projection-API topic registration. The discovery
assertion is the headline fix: the /experiments panels failed with
``unknown_topic`` because no contract exposed
``onex.snapshot.projection.context.experiment-scores.v1``.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote_plus
from uuid import uuid4

import asyncpg
import pytest
import yaml

from omnimarket.events.context_roi import (
    EnumFailureStage,
    ModelAttemptReductionRow,
    ModelContextRoiRunResult,
)
from omnimarket.nodes.node_projection_context_roi.handlers.handler_projection_context_roi import (
    HandlerProjectionContextRoi,
    ModelContextRoiRunCompletedEvent,
)
from omnimarket.projection.discovery import build_projection_topic_map
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

HANDLER = HandlerProjectionContextRoi()
CONTRACT_PATH = Path("src/omnimarket/nodes/node_projection_context_roi/contract.yaml")
EXPERIMENT_SCORES_TOPIC = "onex.snapshot.projection.context.experiment-scores.v1"
RUNNER_TERMINAL_TOPIC = "onex.evt.omnimarket.context-roi-run-completed.v1"
RUNNER_FAILED_TERMINAL_TOPIC = "onex.evt.omnimarket.context-roi-run-failed.v1"


def _row(
    *,
    correlation_id: str,
    subset: str = "golden_exemplar",
    model_id: str = "qwen3-coder-30b",
    first_pass: bool = True,
    final: bool = True,
    prompt_tokens: int = 100,
    completion_tokens: int = 50,
) -> ModelAttemptReductionRow:
    return ModelAttemptReductionRow(
        run_id="run-001",
        correlation_id=correlation_id,
        task_id="task-A",
        run_order=1,
        context_factor_subset=subset,
        context_pack_hash="abc123",
        attempt_count=1,
        first_pass_success=first_pass,
        final_success=final,
        failure_stage=EnumFailureStage.NONE,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        estimated_cost=0.0,
        model_id=model_id,
        provider="local",
        endpoint_ref="local-coder",
    )


class TestContextRoiProjection:
    def test_projects_one_row_per_attempt_reduction_row(self) -> None:
        db = InmemoryDatabaseAdapter()
        result = ModelContextRoiRunResult(
            run_id="run-001",
            rows=(
                _row(correlation_id="c-1", subset="off", model_id="qwen3-coder-30b"),
                _row(correlation_id="c-2", subset="golden_exemplar"),
            ),
        )
        projection = HANDLER.project(result, db)
        assert projection.rows_upserted == 2

        rows = db.query("context_roi_scores")
        assert len(rows) == 2
        first = next(r for r in rows if r["correlation_id"] == "c-1")
        assert first["run_id"] == "run-001"
        assert first["context_factor_subset"] == "off"
        assert first["model_id"] == "qwen3-coder-30b"
        assert first["tokens_used"] == 150
        assert first["final_success"] is True
        assert first["failure_stage"] == "none"
        assert first["proof_class"] == "runtime-observed-only"

    def test_factor_subset_hash_and_routing_source_are_projected_omn14535(
        self,
    ) -> None:
        """OMN-14535: the runner has populated both fields on every row since
        their introduction (handler_context_roi_runner.py's
        _factor_subset_hash / routing_source) — this projection silently
        dropped both, on every row ever written. Drives the real, non-default
        producer values (not the golden-chain fixtures' implicit "" default,
        which never exercised the missing-field bug) through project().
        """
        db = InmemoryDatabaseAdapter()
        row = ModelAttemptReductionRow(
            run_id="run-001",
            correlation_id="c-omn14535",
            task_id="task-A",
            run_order=1,
            context_factor_subset="golden_exemplar",
            context_pack_hash="abc123",
            factor_subset_hash="sha256:real-factor-hash",
            routing_source="routing_tier:local-coder",
            attempt_count=1,
            first_pass_success=True,
            final_success=True,
            failure_stage=EnumFailureStage.NONE,
            prompt_tokens=100,
            completion_tokens=50,
            estimated_cost=0.0,
            model_id="qwen3-coder-30b",
            provider="local",
            endpoint_ref="local-coder",
        )
        result = ModelContextRoiRunResult(run_id="run-001", rows=(row,))

        HANDLER.project(result, db)

        projected = db.query("context_roi_scores")[0]
        assert projected["factor_subset_hash"] == "sha256:real-factor-hash"
        assert projected["routing_source"] == "routing_tier:local-coder"

    def test_tokens_used_is_prompt_plus_completion(self) -> None:
        db = InmemoryDatabaseAdapter()
        result = ModelContextRoiRunResult(
            run_id="run-001",
            rows=(
                _row(
                    correlation_id="c-1",
                    prompt_tokens=320,
                    completion_tokens=80,
                ),
            ),
        )
        HANDLER.project(result, db)
        rows = db.query("context_roi_scores")
        assert rows[0]["tokens_used"] == 400

    def test_upsert_is_idempotent_on_correlation_id(self) -> None:
        db = InmemoryDatabaseAdapter()
        result = ModelContextRoiRunResult(
            run_id="run-001",
            rows=(_row(correlation_id="c-1", final=False),),
        )
        HANDLER.project(result, db)
        # Re-project the same correlation_id with a different outcome.
        result2 = ModelContextRoiRunResult(
            run_id="run-001",
            rows=(_row(correlation_id="c-1", final=True),),
        )
        HANDLER.project(result2, db)
        rows = db.query("context_roi_scores")
        assert len(rows) == 1
        assert rows[0]["final_success"] is True

    def test_handle_strips_transport_metadata(self) -> None:
        db = InmemoryDatabaseAdapter()
        payload: dict[str, object] = {
            "run_id": "run-001",
            "rows": [_row(correlation_id="c-1").model_dump(mode="json")],
            "_db": db,
            "_event_type": "context-roi-run-completed",
            "event_landed": "2026-06-11T00:00:00+00:00",
            "latency_ms": 12,
        }
        out = HANDLER.handle(payload)
        assert out["rows_upserted"] == 1
        rows = db.query("context_roi_scores")
        assert len(rows) == 1
        assert rows[0]["correlation_id"] == "c-1"

    def test_failed_terminal_run_materialises_rows(self) -> None:
        """A fully-failed run delivered on the FAILED terminal still projects.

        Both runner terminals carry the same ModelContextRoiRunResult payload, so
        the failed run's rows — final_success=False, failure_stage=generation —
        materialise into context_roi_scores through the same projection path. This
        is the de-wedge: before subscribing to the failed terminal, a failed run
        produced zero usable rows (OMN-13645).
        """
        db = InmemoryDatabaseAdapter()
        failed_row = ModelAttemptReductionRow(
            run_id="run-failed-001",
            correlation_id="c-fail-1",
            task_id="task-A",
            run_order=1,
            context_factor_subset="golden_exemplar",
            context_pack_hash="abc123",
            attempt_count=2,
            first_pass_success=False,
            final_success=False,
            failure_stage=EnumFailureStage.GENERATION,
            prompt_tokens=120,
            completion_tokens=0,
            estimated_cost=0.0,
            model_id="qwen3-coder-30b",
            provider="local",
            endpoint_ref="local-coder",
        )
        result = ModelContextRoiRunResult(
            run_id="run-failed-001",
            rows=(failed_row,),
            failed_trials=1,
            total_trials=1,
        )
        projection = HANDLER.project(result, db)
        assert projection.rows_upserted == 1

        rows = db.query("context_roi_scores")
        assert len(rows) == 1
        assert rows[0]["correlation_id"] == "c-fail-1"
        assert rows[0]["final_success"] is False
        assert rows[0]["failure_stage"] == "generation"

    def test_handle_projects_failed_terminal_payload(self) -> None:
        """handle() is terminal-agnostic: a failed-terminal envelope projects."""
        db = InmemoryDatabaseAdapter()
        payload: dict[str, object] = {
            "run_id": "run-failed-002",
            "rows": [
                _row(
                    correlation_id="c-fail-2", first_pass=False, final=False
                ).model_dump(mode="json")
            ],
            "_db": db,
            "_event_type": "context-roi-run-failed",
            "event_landed": "2026-06-26T00:00:00+00:00",
            "latency_ms": 8,
        }
        out = HANDLER.handle(payload)
        assert out["rows_upserted"] == 1
        rows = db.query("context_roi_scores")
        assert len(rows) == 1
        assert rows[0]["final_success"] is False

    def test_inbound_event_model_round_trips_runner_payload(self) -> None:
        result = ModelContextRoiRunResult(
            run_id="run-001",
            rows=(_row(correlation_id="c-1"),),
        )
        event = ModelContextRoiRunCompletedEvent(
            **result.model_dump(mode="python", include={"run_id", "rows"})
        )
        assert event.run_id == "run-001"
        assert event.rows[0].correlation_id == "c-1"


class TestContextRoiContractWiring:
    def test_event_bus_wiring(self) -> None:
        contract = yaml.safe_load(CONTRACT_PATH.read_text())
        assert (
            contract["handler"]["module"]
            == "omnimarket.nodes.node_projection_context_roi.handlers."
            "handler_projection_context_roi"
        )
        assert contract["handler"]["class"] == "HandlerProjectionContextRoi"
        assert RUNNER_TERMINAL_TOPIC in contract["event_bus"]["subscribe_topics"]
        # The failed terminal must also be consumed so failed runs are projected
        # instead of wedging the N-arm battery with zero usable rows (OMN-13645).
        assert RUNNER_FAILED_TERMINAL_TOPIC in contract["event_bus"]["subscribe_topics"]
        assert "consumer_group" not in contract["event_bus"], (
            "OMN-15639: event_bus.consumer_group is seam-deleted. The group name is derived from node identity via compute_consumer_group_id(), never declared."
        )
        # OMN-14535: closes a pre-existing state-coverage-gate baseline gap
        # (this node's declared terminal_event output was never asserted by
        # any top-level test) while touching this contract for the dropped-
        # fields fix.
        assert (
            contract["terminal_event"]
            == "onex.evt.omnimarket.projection-context-roi-applied.v1"
        )
        assert contract["terminal_event"] in contract["event_bus"]["publish_topics"]

    def test_migration_declares_handler_schema(self) -> None:
        migration = (
            CONTRACT_PATH.parent / "migrations" / "001_create_context_roi_scores.sql"
        ).read_text()
        assert "CREATE TABLE IF NOT EXISTS context_roi_scores" in migration
        assert "correlation_id TEXT NOT NULL" in migration
        assert "ux_context_roi_scores_identity" in migration
        assert "trg_context_roi_scores_updated_at" in migration
        assert "NEW.updated_at = NOW()" in migration

    def test_projection_api_exposes_experiment_scores_topic(self) -> None:
        """The headline fix: discovery must register the panel topic.

        Without this exposure the /experiments hero + heatmap panels resolve to
        ``unknown_topic`` at the projection API.
        """
        topic_map = build_projection_topic_map()
        assert EXPERIMENT_SCORES_TOPIC in topic_map, (
            f"{EXPERIMENT_SCORES_TOPIC} not registered by projection discovery; "
            "/experiments panels would still fail with unknown_topic"
        )
        cfg = topic_map[EXPERIMENT_SCORES_TOPIC]
        assert cfg.table == "context_roi_scores"
        assert cfg.source_contract == "projection_context_roi"
        # Columns the dashboard heatmap depends on must be exposed.
        for required_column in (
            "model_id",
            "context_factor_subset",
            "final_success",
            "tokens_used",
            "run_id",
        ):
            assert required_column in cfg.columns


def _dsn() -> str:
    password = os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    )
    host = os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost")
    port = os.environ.get("INTEGRATION_POSTGRES_PORT", "5432")
    user = os.environ.get("INTEGRATION_POSTGRES_USER", "postgres")
    db = os.environ.get("INTEGRATION_POSTGRES_DB", "omnibase_infra")
    return f"postgresql://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{db}"


async def _connect_or_skip() -> asyncpg.Connection:
    if not os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    ):
        pytest.skip(
            "no Postgres password set -- skipping the OMN-23628 write-path gate"
        )
    try:
        return await asyncpg.connect(_dsn())
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.skip(f"no reachable Postgres for the OMN-23628 write-path gate: {exc}")
    raise AssertionError("unreachable")


@asynccontextmanager
async def _migrated_omn23628() -> AsyncIterator[tuple[asyncpg.Connection, str]]:
    connection = await _connect_or_skip()
    schema = f"omn23628_{uuid4().hex[:12]}"
    try:
        await connection.execute(f"CREATE SCHEMA {schema}")
        await connection.execute(f"SET search_path TO {schema}")
        migrations_dir = (
            Path(__file__).resolve().parents[1]
            / "src/omnimarket/nodes/node_projection_context_roi/migrations"
        )
        for name in (
            "001_create_context_roi_scores.sql",
            "002_add_factor_subset_hash_and_routing_source.sql",
            "004_add_code_eval_trial_columns.sql",
        ):
            await connection.execute((migrations_dir / name).read_text())
        yield connection, schema
    finally:
        await connection.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await connection.close()


class TestContextRoiProjectionOmni23628:
    def test_replayed_terminal_changes_no_row(self) -> None:
        db = InmemoryDatabaseAdapter()
        row = ModelAttemptReductionRow(
            run_id="run-001",
            correlation_id="c-replay",
            task_id="task-A",
            run_order=1,
            context_factor_subset="golden_exemplar",
            context_pack_hash="abc123",
            attempt_count=1,
            first_pass_success=True,
            final_success=True,
            failure_stage=EnumFailureStage.NONE,
            prompt_tokens=100,
            completion_tokens=50,
            estimated_cost=0.0,
            model_id="qwen3-coder-30b",
            provider="local",
            endpoint_ref="local-coder",
            check_verdict="pass",
            row_kind="try",
            try_end="2026-06-11T00:00:00+00:00",
            suite_digest="sha256:suite",
            size_band="S",
            sequence_id="seq-1",
            try_ordinal=1,
            diff_hash="sha256:diff",
            max_turns=4,
            try_timeout_s=30.0,
            generation_s=12.5,
            env_sync_s=1.0,
            check_s=2.0,
        )
        result = ModelContextRoiRunResult(run_id="run-001", rows=(row,))
        HANDLER.project(result, db)
        first_rows = db.query("context_roi_scores")
        assert len(first_rows) == 1
        first = first_rows[0]
        HANDLER.project(result, db)
        second_rows = db.query("context_roi_scores")
        assert len(second_rows) == 1
        second = second_rows[0]
        for key in first:
            if key == "updated_at":
                continue
            assert first[key] == second[key], (
                f"{key} changed on replay: {first[key]!r} -> {second[key]!r}"
            )

    def test_old_row_still_deserialises(self) -> None:
        db = InmemoryDatabaseAdapter()
        legacy = ModelAttemptReductionRow(
            run_id="run-001",
            correlation_id="c-legacy",
            task_id="task-A",
            run_order=1,
            context_factor_subset="golden_exemplar",
            context_pack_hash="abc123",
            attempt_count=1,
            first_pass_success=True,
            final_success=True,
            failure_stage=EnumFailureStage.NONE,
            prompt_tokens=100,
            completion_tokens=50,
            estimated_cost=0.0,
            model_id="qwen3-coder-30b",
            provider="local",
            endpoint_ref="local-coder",
        )
        result = ModelContextRoiRunResult(run_id="run-001", rows=(legacy,))
        HANDLER.project(result, db)
        row = db.query("context_roi_scores")[0]
        assert row["check_verdict"] is None
        assert row["unscorable_source"] is None
        assert row["row_kind"] == "try"
        assert row["size_band"] == "S"
        assert row["try_ordinal"] == 0
        assert row["max_turns"] == 0
        assert row["try_timeout_s"] == 0.0
        assert row["generation_s"] == 0.0
        assert row["env_sync_s"] == 0.0
        assert row["check_s"] == 0.0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_postgres_write_path_omn23628() -> None:
    async with _migrated_omn23628() as (connection, schema):
        legacy_sql = (
            f"INSERT INTO {schema}.context_roi_scores "
            "(run_id, correlation_id, task_id, run_order, context_factor_subset, "
            "context_pack_hash, attempt_count, first_pass_success, final_success, "
            "failure_stage, prompt_tokens, completion_tokens, estimated_cost, model_id, "
            "provider, endpoint_ref) "
            "VALUES ('run-001', 'c-legacy', 'task-A', 1, 'golden_exemplar', 'abc123', 1, "
            "true, true, 'none', 100, 50, 0.0, 'qwen3-coder-30b', 'local', 'local-coder')"
        )
        await connection.execute(legacy_sql)
        row = await connection.fetchrow(f"SELECT * FROM {schema}.context_roi_scores")
        assert row is not None
        assert row["row_kind"] == "try"
        assert row["size_band"] == "S"
        assert row["try_ordinal"] == 0
        assert row["check_verdict"] is None

        full_sql = (
            f"INSERT INTO {schema}.context_roi_scores "
            "(run_id, correlation_id, task_id, run_order, context_factor_subset, "
            "context_pack_hash, attempt_count, first_pass_success, final_success, "
            "failure_stage, prompt_tokens, completion_tokens, estimated_cost, model_id, "
            "provider, endpoint_ref, check_verdict, unscorable_source, row_kind, try_end, "
            "suite_digest, size_band, sequence_id, try_ordinal, diff_hash, max_turns, "
            "try_timeout_s, generation_s, env_sync_s, check_s) "
            "VALUES ('run-001', 'c-full', 'task-A', 1, 'golden_exemplar', 'abc123', 1, "
            "true, true, 'none', 100, 50, 0.0, 'qwen3-coder-30b', 'local', 'local-coder', "
            "'pass', NULL, 'try', '2026-06-11T00:00:00+00:00', 'sha256:suite', 'S', "
            "'seq-1', 1, 'sha256:diff', 4, 30.0, 12.5, 1.0, 2.0)"
        )
        await connection.execute(full_sql)
        row = await connection.fetchrow(
            f"SELECT * FROM {schema}.context_roi_scores WHERE correlation_id = 'c-full'"
        )
        assert row is not None
        assert row["check_verdict"] == "pass"
        assert row["row_kind"] == "try"
        assert row["try_end"] == "2026-06-11T00:00:00+00:00"
        assert row["suite_digest"] == "sha256:suite"
        assert row["size_band"] == "S"
        assert row["sequence_id"] == "seq-1"
        assert row["try_ordinal"] == 1
        assert row["diff_hash"] == "sha256:diff"
        assert row["max_turns"] == 4
        assert row["try_timeout_s"] == 30.0
        assert row["generation_s"] == 12.5
        assert row["env_sync_s"] == 1.0
        assert row["check_s"] == 2.0

        upsert_sql = (
            f"INSERT INTO {schema}.context_roi_scores "
            "(run_id, correlation_id, task_id, run_order, context_factor_subset, "
            "context_pack_hash, attempt_count, first_pass_success, final_success, "
            "failure_stage, prompt_tokens, completion_tokens, estimated_cost, model_id, "
            "provider, endpoint_ref) "
            "VALUES ('run-001', 'c-conflict', 'task-A', 1, 'golden_exemplar', 'abc123', 1, "
            "true, true, 'none', 100, 50, 0.0, 'qwen3-coder-30b', 'local', 'local-coder') "
            "ON CONFLICT (correlation_id) DO UPDATE SET final_success = EXCLUDED.final_success"
        )
        await connection.execute(upsert_sql)
        await connection.execute(upsert_sql)
        count = await connection.fetchval(
            f"SELECT count(*) FROM {schema}.context_roi_scores WHERE correlation_id = 'c-conflict'"
        )
        assert count == 1
