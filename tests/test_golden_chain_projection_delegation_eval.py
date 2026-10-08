# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19790: label replacement, rule-7a dispatch, and tenant-safe persistence."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_projection_delegation_eval.handlers import (
    DelegationEvalProjectionWriter,
    HandlerProjectionDelegationEval,
)
from omnimarket.nodes.node_projection_delegation_eval.models import (
    ModelDelegationEvalProjectionRequest,
)
from omnimarket.projection.runner import BaseProjectionRunner

pytestmark = pytest.mark.unit
_IN_TOPIC = "onex.evt.omnimarket.delegation-eval-item-labelled.v1"  # onex-topic-allow: asserted against the contract
_OUT_TOPIC = "onex.evt.omnimarket.projection-delegation-eval-applied.v1"  # onex-topic-allow: asserted against the contract
_DLQ_TOPIC = "onex.dlq.omnimarket.projection-delegation-eval-malformed.v1"  # onex-topic-allow: asserted against the contract
_RUN_TOPIC = "onex.evt.omnimarket.delegation-eval-run-completed.v1"  # onex-topic-allow: asserted against the contract
_SNAPSHOT_TOPIC = "onex.snapshot.projection.delegation.acceptance-eval.v1"  # onex-topic-allow: asserted against the contract
_NODE = (
    Path(__file__).resolve().parents[1]
    / "src/omnimarket/nodes/node_projection_delegation_eval"
)
_T0 = datetime(2026, 9, 28, tzinfo=UTC)
_TENANT = "11111111-1111-1111-1111-111111111111"


def _event(**updates: Any) -> dict[str, Any]:
    return {
        "tenant_id": _TENANT,
        "correlation_id": "call-1",
        "attempt_index": 0,
        "task_class": "code",
        "stratum": "hard",
        "prompt_snapshot": "explain this",
        "response_snapshot": "an explanation",
        "gate_verdict": "pass",
        "deciding_check": "tests",
        "label": "correct",
        "rater_role": "human",
        "rubric_version": "v1",
        "computed_facts": {"passed": True},
        "observed_at": _T0,
    } | updates


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
        assert "ON CONFLICT (tenant_id, item_key, rater_role, rubric_version)" in query
        assert "observed_at < EXCLUDED.observed_at" in query
        assert "label = EXCLUDED.label" in query
        assert "RETURNING" in query
        self.queries.append(query)
        columns = query.split("(", 1)[1].split(")", 1)[0].replace("\n", "").split(",")
        row = dict(
            zip([c.strip() for c in columns][: len(params)], params, strict=True)
        )
        assert isinstance(row["tenant_id"], UUID)
        assert tenant == str(row["tenant_id"])
        assert isinstance(row["observed_at"], datetime)
        row["computed_facts"] = json.loads(row["computed_facts"])
        key = tuple(
            row[c] for c in ("tenant_id", "item_key", "rater_role", "rubric_version")
        )
        if key in self.rows and self.rows[key]["observed_at"] >= row["observed_at"]:
            return []
        self.rows[key] = row
        return [{"projection_cursor": len(self.rows)}]


def _writer() -> tuple[DelegationEvalProjectionWriter, _Store]:
    writer = DelegationEvalProjectionWriter()
    store = _Store()
    writer._db = store  # type: ignore[assignment]
    return writer, store


def test_one_row_per_label_key() -> None:
    writer, store = _writer()
    assert writer.handle(_event())["rows_upserted"] == 1
    key = (UUID(_TENANT), "call-1:0", "human", "v1")
    assert list(store.rows) == [key]
    assert (
        writer.handle(
            _event(label="incorrect", observed_at=_T0 + timedelta(seconds=1))
        )["rows_upserted"]
        == 1
    )
    assert len(store.rows) == 1
    assert store.rows[key]["label"] == "incorrect"
    assert writer.handle(_event(rater_role="judge"))["rows_upserted"] == 1
    assert len(store.rows) == 2


@pytest.mark.parametrize(
    "updates",
    [{"rubric_version": "v2"}, {"tenant_id": "22222222-2222-2222-2222-222222222222"}],
)
def test_distinct_rubrics_and_tenants_are_separate(updates: dict[str, Any]) -> None:
    writer, store = _writer()
    writer.handle(_event())
    writer.handle(_event(**updates))
    assert len(store.rows) == 2


def test_stale_and_duplicate_labels_write_zero_rows() -> None:
    writer, store = _writer()
    writer.handle(_event())
    for observed_at in (_T0, _T0 - timedelta(seconds=1)):
        assert (
            writer.handle(_event(label="stale", observed_at=observed_at))[
                "rows_upserted"
            ]
            == 0
        )
    assert next(iter(store.rows.values()))["label"] == "correct"
    assert store.loop is None


def test_the_contract_declares_the_topics_the_writer_subscribes() -> None:
    contract = yaml.safe_load((_NODE / "contract.yaml").read_text())
    assert contract["event_bus"] == {
        "subscribe_topics": [_IN_TOPIC, _RUN_TOPIC],
        "publish_topics": [_OUT_TOPIC, _SNAPSHOT_TOPIC],
        "dlq_topics": [_DLQ_TOPIC],
    }
    assert contract["terminal_event"] == _OUT_TOPIC
    assert _writer()[0].subscribe_topics == [_IN_TOPIC, _RUN_TOPIC]
    table = contract["db_io"]["db_tables"][0]
    assert (table["schema"], table["name"], table["access"]) == (
        "public",
        "delegation_eval_items",
        "read_write",
    )
    # OMN-19833: only the writer is routed; it calls the pure fold in process.
    assert {h["handler"]["name"] for h in contract["handler_routing"]["handlers"]} == {
        "DelegationEvalProjectionWriter",
    }


def test_the_writer_declares_in_process_dispatch() -> None:
    assert issubclass(DelegationEvalProjectionWriter, BaseProjectionRunner)
    assert DelegationEvalProjectionWriter.onex_runtime_inprocess_dispatch is True


def test_the_writer_entry_returns_a_row_count() -> None:
    writer, _ = _writer()
    assert isinstance(writer._derive, HandlerProjectionDelegationEval)
    result = writer.handle(_event())
    assert type(result["rows_upserted"]) is int
    # Prompt/response content belongs only in the lab table, never the applied event.
    assert "prompt_snapshot" not in json.dumps(result)
    assert "response_snapshot" not in json.dumps(result)


def test_fold_determinism_and_envelope_timestamp() -> None:
    event = _event()
    event["_envelope_timestamp"] = event.pop("observed_at")
    request = ModelDelegationEvalProjectionRequest.model_validate(event)
    fold = HandlerProjectionDelegationEval()
    assert fold.handle(request) == fold.handle(request)
    row = fold.handle(request).rows[0]
    assert row.item_key == "call-1:0"
    assert row.observed_at == _T0
    assert row.computed_facts == {"passed": True}


def test_missing_event_time_and_tenant_fail_closed() -> None:
    for field in ("observed_at", "tenant_id"):
        event = _event()
        del event[field]
        with pytest.raises(ValidationError):
            ModelDelegationEvalProjectionRequest.model_validate(event)


def test_migrations_enforce_tenant_isolation_and_writer_grants() -> None:
    ddl = (_NODE / "migrations/0000_create_delegation_eval_items.sql").read_text()
    assert "tenant_id UUID NOT NULL" in ddl
    assert "PRIMARY KEY (tenant_id, item_key, rater_role, rubric_version)" in ddl
    assert "ENABLE ROW LEVEL SECURITY" in ddl
    # FORCE lives in its own fenced migration (0002): the fence runner skips a
    # whole file, so it cannot share one with the CREATE TABLE.
    assert "FORCE ROW LEVEL SECURITY" not in ddl
    force = (_NODE / "migrations/0002_force_rls_delegation_eval_items.sql").read_text()
    assert "ALTER TABLE public.delegation_eval_items FORCE ROW LEVEL SECURITY" in force
    assert "CREATE POLICY tenant_isolation" in ddl
    assert "USING (tenant_id = current_setting('app.tenant_id', true)::uuid)" in ddl
    assert (
        "WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid)" in ddl
    )
    grants = (
        _NODE
        / "migrations/0001_grant_tenant_projection_writer_delegation_eval_items.sql"
    ).read_text()
    assert "GRANT SELECT, INSERT, UPDATE" in grants
    assert "ON SEQUENCE public.delegation_eval_items_projection_cursor_seq" in grants
    assert "TO tenant_projection_writer" in grants
    assert "has_sequence_privilege(" in grants


# ---------------------------------------------------------------------------
# OMN-19793 (EV.4): the run operation and its two tables.
# ---------------------------------------------------------------------------


def _gate_items() -> tuple[Any, ...]:
    from omnimarket.events.delegation_gate_eval.model_gate_eval_item import (
        ModelGateEvalItem,
    )

    rows = []
    for index, (task_class, recorded, label) in enumerate(
        [
            ("summarization", "accepted", "adequate"),
            ("summarization", "accepted", "inadequate"),
            ("summarization", "refused", "adequate"),
            ("planning", "accepted", "inadequate"),
            ("planning", "refused", "inadequate"),
        ]
    ):
        rows.append(
            ModelGateEvalItem(
                item_id=f"call-{index}:0",
                task_class=task_class,
                stratum=f"{task_class}/{recorded}",
                label=label,
                prompt_text=f"prompt {index}",
                recorded_answer=f"answer {index}",
                recorded_verdict=recorded,
                recorded_deciding_check="numbers_grounded"
                if recorded == "refused"
                else None,
            )
        )
    return tuple(rows)


class _FixedItems:
    def __init__(self, items: tuple[Any, ...]) -> None:
        self.items = items
        self.calls: list[tuple[str, str, str]] = []

    def get_labelled_items(
        self, manifest_id: str, rater_role: str, rubric_version: str
    ) -> tuple[Any, ...]:
        self.calls.append((manifest_id, rater_role, rubric_version))
        return self.items


def _stub_gate(item: Any) -> Any:
    from omnimarket.events.delegation_gate_eval.model_gate_replay_verdict import (
        ModelGateReplayVerdict,
    )

    return ModelGateReplayVerdict(verdict="accepted")


def _run_payload(
    items: tuple[Any, ...], *, gate_version: str = "gate-1", at: datetime = _T0
) -> dict[str, Any]:
    from omnimarket.events.delegation_eval import ModelDelegationEvalRunRequest
    from omnimarket.nodes.node_delegation_eval_run_orchestrator.handlers import (
        HandlerDelegationEvalRun,
    )
    from omnimarket.nodes.node_delegation_gate_eval_compute.handlers.handler_delegation_gate_eval import (
        HandlerDelegationGateEval,
    )

    handler = HandlerDelegationEvalRun(
        _FixedItems(items), HandlerDelegationGateEval(_stub_gate), clock=lambda: at
    )
    request = ModelDelegationEvalRunRequest(
        tenant_id=UUID(_TENANT),
        manifest_id="manifest-1",
        rater_role="blind_model",
        rubric_version="ev4-blind-v1",
        gate_version=gate_version,
    )
    return handler.handle(request).payload.model_dump(mode="json")


class _RunStore:
    """Loop-bound double for the two run tables, keyed by each table's conflict key."""

    def __init__(self) -> None:
        self.tables: dict[str, dict[tuple[Any, ...], dict[str, Any]]] = {}
        self.loop: asyncio.AbstractEventLoop | None = None

    async def connect(self) -> None:
        self.loop = asyncio.get_running_loop()

    async def close(self) -> None:
        self.loop = None

    async def execute(
        self, query: str, *params: Any, tenant: str | None = None
    ) -> list[dict[str, Any]]:
        assert self.loop is asyncio.get_running_loop()
        assert "RETURNING" in query
        assert "observed_at < EXCLUDED.observed_at" in query
        table = query.split("INSERT INTO public.", 1)[1].split(" ", 1)[0]
        columns = [
            c.strip()
            for c in query.split("(", 1)[1]
            .split(")", 1)[0]
            .replace("\n", "")
            .split(",")
        ]
        row = dict(zip(columns[: len(params)], params, strict=True))
        assert tenant == str(row["tenant_id"])
        assert "prompt" not in " ".join(columns)
        conflict = [
            c.strip()
            for c in query.split("ON CONFLICT (", 1)[1].split(")", 1)[0].split(",")
        ]
        key = tuple(row[c] for c in conflict)
        rows = self.tables.setdefault(table, {})
        if key in rows and rows[key]["observed_at"] >= row["observed_at"]:
            return []
        rows[key] = row
        return [{"projection_cursor": len(rows)}]


def _run_writer() -> tuple[DelegationEvalProjectionWriter, _RunStore]:
    writer = DelegationEvalProjectionWriter()
    store = _RunStore()
    writer._db = store  # type: ignore[assignment]
    return writer, store


def test_run_completed_writes_verdict_and_results_rows() -> None:
    payload = _run_payload(_gate_items())
    assert payload["status"] == "completed"
    writer, store = _run_writer()
    applied = writer.handle(payload)
    verdicts = store.tables["delegation_eval_item_verdicts"]
    results = store.tables["delegation_eval_results"]
    assert len(verdicts) == 5
    # Two classes; strata "all" plus accepted and refused; two arms each. Only
    # summarization has a class rubric, so it alone adds a rubric arm (OMN-20166).
    assert len(results) == 2 * 3 * 2 + 3
    assert {key[2] for key in results if key[4] == "rubric"} == {"summarization"}
    assert applied["rows_upserted"] == len(verdicts) + len(results)
    recorded_all = {
        key[2]: row
        for key, row in results.items()
        if key[3] == "all" and key[4] == "recorded"
    }
    assert recorded_all["summarization"]["accepted_n"] == 2
    assert recorded_all["summarization"]["false_pass_count"] == 1
    assert recorded_all["summarization"]["false_refusal_count"] == 1
    assert recorded_all["planning"]["false_pass_count"] == 1
    # Under-powered: never MET.
    assert recorded_all["planning"]["false_pass_line_verdict"] == "refused"
    item = verdicts[(UUID(_TENANT), UUID(payload["eval_run_id"]), "call-2:0")]
    assert (item["recorded_verdict"], item["replayed_verdict"]) == (
        "refused",
        "accepted",
    )


def test_redelivered_run_writes_the_same_rows_and_no_more() -> None:
    writer, store = _run_writer()
    first = _run_payload(_gate_items(), at=_T0)
    writer.handle(first)
    before = {t: dict(rows) for t, rows in store.tables.items()}
    redelivered = _run_payload(_gate_items(), at=_T0 + timedelta(minutes=5))
    assert redelivered["eval_run_id"] == first["eval_run_id"]
    writer.handle(redelivered)
    assert {t: set(rows) for t, rows in store.tables.items()} == {
        t: set(rows) for t, rows in before.items()
    }
    strip = lambda row: {k: v for k, v in row.items() if k != "observed_at"}  # noqa: E731
    for table, rows in store.tables.items():
        for key, row in rows.items():
            assert strip(row) == strip(before[table][key])
    # A gate change is a new run, beside the old one.
    writer.handle(_run_payload(_gate_items(), gate_version="gate-2"))
    assert len(store.tables["delegation_eval_item_verdicts"]) == 10


def test_run_completed_row_count_never_zero_for_a_non_empty_result() -> None:
    writer, _ = _run_writer()
    payload = _run_payload(_gate_items())
    first = writer.handle(payload)
    assert first["rows_upserted"] > 0
    duplicate = writer.handle(payload)
    assert duplicate["rows_upserted"] == 0
    assert duplicate["rows_refused_by_ordering_guard"] == first["rows_upserted"]
    assert (
        duplicate["verdict_rows"] + duplicate["result_rows"] == first["rows_upserted"]
    )


def test_a_failed_run_is_one_terminal_and_writes_no_rows() -> None:
    payload = _run_payload(())
    assert payload["status"] == "failed"
    assert payload["failure_reasons"]
    writer, store = _run_writer()
    applied = writer.handle(payload)
    assert applied["rows_upserted"] == 0
    assert applied["verdict_rows"] == applied["result_rows"] == 0
    assert store.tables == {}


def test_the_run_payload_is_content_free() -> None:
    body = json.dumps(_run_payload(_gate_items()))
    assert "prompt 0" not in body
    assert "answer 0" not in body


def test_the_contract_declares_the_run_topic_tables_and_snapshot() -> None:
    contract = yaml.safe_load((_NODE / "contract.yaml").read_text())
    assert _RUN_TOPIC in contract["event_bus"]["subscribe_topics"]
    assert _RUN_TOPIC in _writer()[0].subscribe_topics
    names = {t["name"] for t in contract["db_io"]["db_tables"]}
    assert {"delegation_eval_item_verdicts", "delegation_eval_results"} <= names
    (exposure,) = contract["projection_api"]["exposures"]
    assert exposure["topic"] == _SNAPSHOT_TOPIC
    assert exposure["table"] == "delegation_eval_results"
    assert exposure["tenant_column"] == "tenant_id"


def test_run_migrations_enforce_tenant_isolation_and_writer_grants() -> None:
    for name, table in (
        (
            "0003_create_delegation_eval_item_verdicts.sql",
            "delegation_eval_item_verdicts",
        ),
        ("0004_create_delegation_eval_results.sql", "delegation_eval_results"),
    ):
        ddl = (_NODE / "migrations" / name).read_text()
        assert "tenant_id UUID NOT NULL" in ddl
        assert f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY" in ddl
        assert "FORCE ROW LEVEL SECURITY" not in ddl
        assert (
            "WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid)"
            in ddl
        )
    force = (
        _NODE / "migrations/0006_force_rls_delegation_eval_run_tables.sql"
    ).read_text()
    grants = (
        _NODE
        / "migrations/0005_grant_tenant_projection_writer_delegation_eval_run_tables.sql"
    ).read_text()
    for table in ("delegation_eval_item_verdicts", "delegation_eval_results"):
        assert f"ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY" in force
        assert f"ON SEQUENCE public.{table}_projection_cursor_seq" in grants


def test_the_run_node_has_one_command_and_one_terminal() -> None:
    """The run operation is its own node, so its command has exactly one terminal."""
    contract = yaml.safe_load(
        (
            _NODE.parent / "node_delegation_eval_run_orchestrator" / "contract.yaml"
        ).read_text()
    )
    command = "onex.cmd.omnimarket.delegation-eval-run.v1"
    assert contract["event_bus"]["subscribe_topics"] == [command]
    assert contract["runtime_dispatch"]["command_topic"] == command
    assert contract["event_bus"]["publish_topics"] == [_RUN_TOPIC]
    assert contract["terminal_event"] == _RUN_TOPIC
    assert contract["runtime_dispatch"]["terminal_events"]["success"] == _RUN_TOPIC
