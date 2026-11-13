# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19998: experiment-arm-result projection, rule-7a dispatch, and tenant-safe persistence."""

from __future__ import annotations

import asyncio
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.models.experiment_result.model_experiment_arm_result import (
    ModelExperimentArmResult,
)
from omnimarket.nodes.node_experiment_result_record_compute.models import (
    EnumExperimentResultRecordStatus,
    ModelExperimentResultRecordResult,
)
from omnimarket.nodes.node_projection_experiment_result.handlers import (
    ExperimentResultProjectionWriter,
    HandlerProjectionExperimentResult,
)
from omnimarket.nodes.node_projection_experiment_result.models import (
    ModelExperimentResultProjectionRequest,
    ModelExperimentResultRow,
)
from omnimarket.projection.runner import BaseProjectionRunner

pytestmark = pytest.mark.unit

_IN_TOPIC = "onex.evt.omnimarket.experiment-arm-result-recorded.v1"  # onex-topic-allow: asserted against the contract
_OUT_TOPIC = "onex.evt.omnimarket.projection-experiment-result-applied.v1"  # onex-topic-allow: asserted against the contract
_DLQ_TOPIC = "onex.dlq.omnimarket.projection-experiment-result-malformed.v1"  # onex-topic-allow: asserted against the contract
_SNAPSHOT_TOPIC = "onex.snapshot.projection.eval.experiment-results.v1"  # onex-topic-allow: asserted against the contract
_NODE = (
    Path(__file__).resolve().parents[1]
    / "src/omnimarket/nodes/node_projection_experiment_result"
)
_T0 = datetime(2026, 9, 28, tzinfo=UTC)
_TENANT = "11111111-1111-1111-1111-111111111111"
_RESULT_ID = "9e7cd34a-3f2b-4c5d-8e6f-1a2b3c4d5e6f"
_CONFLICT_KEY = ("tenant_id", "experiment_id", "arm", "model_id", "measured_at")


def _wilson_interval(correct: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Independent Wilson 95% interval helper used only by the test."""
    p = correct / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return centre - half, centre + half


def _result(**updates: Any) -> dict[str, Any]:
    """One ModelExperimentArmResult-shaped dict; the Wilson interval recomputed."""
    n = updates.get("n", 60)
    correct = updates.get("correct", 48)
    wilson_low, wilson_high = _wilson_interval(correct, n)
    return {
        "tenant_id": _TENANT,
        "experiment_id": "exp-1",
        "arm": "arm-a",
        "model_id": "model-x",
        "engine": "vllm",
        "n": n,
        "correct": correct,
        "wilson_low": wilson_low,
        "wilson_high": wilson_high,
        "baseline_accuracy": 0.7,
        "latency_p50_ms": 12.5,
        "latency_p90_ms": 40.0,
        "gpu_seconds": 300.0,
        "corpus_sha256": "c" * 64,
        "truth_sha256": "a" * 64,
        "report_ref": "",
        "measured_at": _T0,
    } | updates


def _event(accuracy: float = 0.8, **updates: Any) -> dict[str, Any]:
    """The recorded terminal the writer receives, derived from its producer's model.

    The payload is the record node's own output model dumped in JSON mode, so a
    change to that model changes this fixture. The envelope ``accuracy`` is
    pinned to 0.8 regardless of the arm's counts, so a test that sees the derived
    accuracy can tell it was computed from ``result`` and not read off the envelope.
    """
    record = ModelExperimentResultRecordResult(
        status=EnumExperimentResultRecordStatus.COMPLETED,
        result_id=UUID(_RESULT_ID),
        result=ModelExperimentArmResult.model_validate(_result(**updates)),
        accuracy=accuracy,
    )
    return record.model_dump(mode="json")


class _Store:
    """Loop-bound adapter double; validates SQL grain and typed DB arguments."""

    def __init__(self) -> None:
        self.rows: dict[tuple[Any, ...], dict[str, Any]] = {}
        self.loop: asyncio.AbstractEventLoop | None = None
        self.queries: list[str] = []

    async def connect(self) -> None:
        assert self.loop is None
        self.loop = asyncio.get_running_loop()

    async def close(self) -> None:
        self.loop = None

    async def execute(
        self, query: str, *params: Any, tenant: str | None = None
    ) -> list[dict[str, Any]]:
        assert self.loop is asyncio.get_running_loop()
        assert (
            "ON CONFLICT (tenant_id, experiment_id, arm, model_id, measured_at)"
            in query
        )
        assert "DO NOTHING" in query
        assert "RETURNING" in query
        self.queries.append(query)
        columns = [
            c.strip()
            for c in query.split("(", 1)[1]
            .split(")", 1)[0]
            .replace("\n", "")
            .split(",")
        ]
        row = dict(zip(columns[: len(params)], params, strict=True))
        assert isinstance(row["tenant_id"], UUID)
        assert tenant == str(row["tenant_id"])
        assert isinstance(row["measured_at"], datetime)
        assert isinstance(row["n"], int)
        assert not isinstance(row["n"], bool)
        assert isinstance(row["correct"], int)
        assert not isinstance(row["correct"], bool)
        assert isinstance(row["accuracy"], float)
        assert isinstance(row["clears_baseline"], bool)
        key = tuple(row[c] for c in _CONFLICT_KEY)
        if key in self.rows:
            return []  # a redelivery conflicts and changes nothing
        self.rows[key] = row
        return [{"projection_cursor": len(self.rows)}]


def _writer() -> tuple[ExperimentResultProjectionWriter, _Store]:
    writer = ExperimentResultProjectionWriter()
    store = _Store()
    cast(Any, writer)._db = store
    return writer, store


class _Publisher:
    """Stands in for publish_snapshot_delta and records every call's kwargs."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, exposure: Any, **kwargs: Any) -> bool:
        del exposure
        self.calls.append(kwargs)
        return True


def test_golden_one_row_and_derived_metrics() -> None:
    writer, store = _writer()
    result = writer.handle(_event())
    assert result["rows_upserted"] == 1
    assert result["rows_refused_by_ordering_guard"] == 0
    assert result["experiment_id"] == "exp-1"
    assert result["arm"] == "arm-a"
    key = (UUID(_TENANT), "exp-1", "arm-a", "model-x", _T0)
    assert list(store.rows) == [key]
    row = store.rows[key]
    wilson_low, wilson_high = _wilson_interval(48, 60)
    assert row["accuracy"] == pytest.approx(0.8)
    assert row["advantage_over_baseline"] == pytest.approx(0.1)
    # wilson_low ~0.68 sits below the 0.7 majority-answer baseline.
    assert row["clears_baseline"] is False
    assert row["wilson_low"] == pytest.approx(wilson_low)
    assert row["wilson_high"] == pytest.approx(wilson_high)
    # 58 of 60 sits far enough above the same baseline for the WHOLE interval
    # to clear it -- and the derived accuracy proves it was computed from the
    # result, not read off the envelope's pinned 0.8.
    writer2, store2 = _writer()
    writer2.handle(_event(correct=58))
    (row2,) = store2.rows.values()
    assert row2["accuracy"] == pytest.approx(58 / 60)
    assert row2["advantage_over_baseline"] == pytest.approx(58 / 60 - 0.7)
    assert row2["clears_baseline"] is True


def test_a_redelivered_event_refuses_and_changes_nothing() -> None:
    writer, store = _writer()
    first = writer.handle(_event())
    assert first["rows_upserted"] == 1
    second = writer.handle(_event())
    assert second["rows_upserted"] == 0
    assert second["rows_refused_by_ordering_guard"] == 1
    assert len(store.rows) == 1
    assert store.loop is None


def test_a_later_measured_at_is_a_new_row() -> None:
    writer, store = _writer()
    writer.handle(_event())
    writer.handle(_event(measured_at=_T0 + timedelta(hours=1), report_ref="run-2"))
    assert len(store.rows) == 2
    old_key = (UUID(_TENANT), "exp-1", "arm-a", "model-x", _T0)
    new_key = (
        UUID(_TENANT),
        "exp-1",
        "arm-a",
        "model-x",
        _T0 + timedelta(hours=1),
    )
    assert set(store.rows) == {old_key, new_key}
    assert store.rows[old_key]["accuracy"] == pytest.approx(0.8)
    assert store.rows[new_key]["report_ref"] == "run-2"


def test_another_tenant_is_a_separate_row() -> None:
    writer, store = _writer()
    writer.handle(_event())
    writer.handle(_event(tenant_id="22222222-2222-2222-2222-222222222222"))
    assert len(store.rows) == 2


def test_a_malformed_event_fails_closed_and_writes_no_row() -> None:
    writer, store = _writer()
    missing = _event()
    del missing["result"]["corpus_sha256"]
    with pytest.raises(ValidationError):
        writer.handle(missing)
    assert store.rows == {}
    assert store.loop is None
    malformed = _event()
    malformed["result"]["corpus_sha256"] = "not-a-hash"
    with pytest.raises(ValidationError):
        writer.handle(malformed)
    assert store.rows == {}
    assert store.loop is None


def test_one_snapshot_delta_per_new_row_and_none_for_a_refused_redelivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writer, store = _writer()
    publisher = _Publisher()
    monkeypatch.setattr(writer, "publish_snapshot_delta", publisher)
    first = writer.handle(_event())
    assert first["rows_upserted"] == 1
    assert len(publisher.calls) == 1
    call = publisher.calls[0]
    assert call["op"] == "upsert"
    assert call["row"]["arm"] == "arm-a"
    assert call["row"]["accuracy"] == pytest.approx(0.8)
    assert datetime.fromisoformat(call["row"]["measured_at"]) == _T0
    assert call["tenant_id"] == _TENANT
    assert call["source_topic"] == _IN_TOPIC
    assert call["source_event_id"] == ""
    second = writer.handle(_event())
    assert second["rows_refused_by_ordering_guard"] == 1
    assert len(publisher.calls) == 1
    assert len(store.rows) == 1


def test_the_contract_declares_the_topics_the_writer_subscribes() -> None:
    contract = yaml.safe_load((_NODE / "contract.yaml").read_text())
    assert contract["event_bus"] == {
        "subscribe_topics": [_IN_TOPIC],
        "publish_topics": [_OUT_TOPIC, _SNAPSHOT_TOPIC],
        "dlq_topics": [_DLQ_TOPIC],
    }
    assert contract["terminal_event"] == _OUT_TOPIC
    assert _writer()[0].subscribe_topics == [_IN_TOPIC]
    table = contract["db_io"]["db_tables"][0]
    assert (table["schema"], table["name"], table["access"]) == (
        "public",
        "experiment_arm_results",
        "read_write",
    )
    # Only the writer is routed; it calls the pure fold in process.
    assert {h["handler"]["name"] for h in contract["handler_routing"]["handlers"]} == {
        "ExperimentResultProjectionWriter",
    }


def test_the_writer_declares_in_process_dispatch() -> None:
    assert issubclass(ExperimentResultProjectionWriter, BaseProjectionRunner)
    assert ExperimentResultProjectionWriter.onex_runtime_inprocess_dispatch is True


def test_the_contract_columns_match_the_row_model() -> None:
    contract = yaml.safe_load((_NODE / "contract.yaml").read_text())
    (exposure,) = contract["projection_api"]["exposures"]
    expected = [*ModelExperimentResultRow.model_fields, "projection_cursor"]
    assert exposure["columns"] == expected
    assert exposure["topic"] == _SNAPSHOT_TOPIC
    assert exposure["bus_backed"] is True
    assert exposure["key_columns"] == [
        "tenant_id",
        "experiment_id",
        "arm",
        "model_id",
        "measured_at",
    ]
    assert exposure["tenant_column"] == "tenant_id"
    assert exposure["consumers"] == "none"
    assert "Model tests tab" in exposure["consumers_reason"]


def test_sql_seam_matches_the_writer_insert_columns() -> None:
    ddl = (_NODE / "migrations/0000_create_experiment_arm_results.sql").read_text()
    create = ddl.split("CREATE TABLE IF NOT EXISTS public.experiment_arm_results (", 1)[
        1
    ].split(");", 1)[0]
    table_columns = set()
    for line in create.splitlines():
        line = line.strip()
        if not line or line.startswith("PRIMARY KEY"):
            continue
        table_columns.add(line.split()[0].rstrip(","))
    writer_src = (_NODE / "handlers/handler_experiment_result_writer.py").read_text()
    insert = writer_src.split("INSERT INTO public.experiment_arm_results (", 1)[1]
    insert_columns = {
        c.strip() for c in insert.split(")", 1)[0].replace("\n", "").split(",")
    }
    assert table_columns == insert_columns | {"projection_cursor"}
    assert table_columns - insert_columns == {"projection_cursor"}
    assert insert_columns <= table_columns


def test_migrations_enforce_tenant_isolation_and_writer_grants() -> None:
    ddl = (_NODE / "migrations/0000_create_experiment_arm_results.sql").read_text()
    assert "tenant_id UUID NOT NULL" in ddl
    assert "PRIMARY KEY (tenant_id, experiment_id, arm, model_id, measured_at)" in ddl
    # FORCE lives in its own fenced migration (0002): the fence runner skips a
    # whole file, so it cannot share one with the CREATE TABLE.
    assert "FORCE ROW LEVEL SECURITY" not in ddl
    force = (_NODE / "migrations/0002_force_rls_experiment_arm_results.sql").read_text()
    assert "ALTER TABLE public.experiment_arm_results FORCE ROW LEVEL SECURITY" in force
    assert "CREATE POLICY tenant_isolation" in force
    assert "USING (tenant_id = current_setting('app.tenant_id', true)::uuid)" in force
    assert (
        "WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid)" in force
    )
    assert "GRANT SELECT ON public.experiment_arm_results TO app_dashboard" in force
    grants = (
        _NODE
        / "migrations/0001_grant_tenant_projection_writer_experiment_arm_results.sql"
    ).read_text()
    assert "GRANT SELECT, INSERT, UPDATE" in grants
    assert "ON SEQUENCE public.experiment_arm_results_projection_cursor_seq" in grants
    assert "TO tenant_projection_writer" in grants
    assert "has_sequence_privilege(" in grants


def test_control_the_fold_never_reads_a_clock() -> None:
    fold = HandlerProjectionExperimentResult()
    request = ModelExperimentResultProjectionRequest.model_validate(_event())
    assert fold.handle(request) == fold.handle(request)


def test_control_clears_baseline_is_the_interval_not_the_point() -> None:
    fold = HandlerProjectionExperimentResult()
    low = fold.handle(
        ModelExperimentResultProjectionRequest.model_validate(
            _event(wilson_low=0.55, wilson_high=0.80)
        )
    )
    # accuracy 0.80 exceeds baseline 0.70, but the interval does not clear it.
    assert low.row.accuracy == pytest.approx(0.8)
    assert low.row.clears_baseline is False
    high = fold.handle(
        ModelExperimentResultProjectionRequest.model_validate(
            _event(wilson_low=0.72, wilson_high=0.80)
        )
    )
    assert high.row.clears_baseline is True


def test_the_fold_copies_the_result_and_derives_the_three_fields() -> None:
    payload = _event(correct=58, baseline_accuracy=0.7)
    request = ModelExperimentResultProjectionRequest.model_validate(payload)
    row = HandlerProjectionExperimentResult().handle(request).row
    for name, value in request.result.model_dump().items():
        assert getattr(row, name) == value
    assert row.accuracy == pytest.approx(58 / 60)
    assert row.advantage_over_baseline == pytest.approx(58 / 60 - 0.7)
    assert row.clears_baseline is True


def test_control_accuracy_is_recomputed_from_the_counts_not_read_off_the_envelope() -> (
    None
):
    # The envelope says 0.8; the counts say 30 of 60. The fold must read the counts.
    payload = _event(correct=30)
    row = (
        HandlerProjectionExperimentResult()
        .handle(ModelExperimentResultProjectionRequest.model_validate(payload))
        .row
    )
    assert payload["accuracy"] == pytest.approx(0.8)
    assert row.accuracy == pytest.approx(0.5)
