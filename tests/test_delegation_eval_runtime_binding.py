# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20577: the delegation eval nodes get a tenant-scoped runtime binding.

Golden chain: a delivered eval-run (or label-record) command, handled by the handler the
contract routes to and constructed with no arguments, as the runtime constructs it, no longer
raises the missing-source RuntimeError; it reads the calling tenant's rows only.
Error chain: an unconfigured or unreadable source ends the run in a failed terminal.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
import yaml

from omnimarket.events.delegation_eval import (
    ModelDelegationEvalRunRequest,
    ModelLabelRecordRequest,
)
from omnimarket.events.delegation_gate_eval.model_gate_replay_verdict import (
    ModelGateReplayVerdict,
)
from omnimarket.handlers import delegation_eval_read_binding as binding
from omnimarket.handlers import tenant_scoped_row_reader as reader_module
from omnimarket.handlers.node_overlay_reader import OVERLAY_ROOTS_ENV
from omnimarket.nodes.node_delegation_eval_orchestrator.handlers.handler_delegation_eval_orchestrator_runtime import (
    HandlerDelegationEvalOrchestratorRuntime,
)
from omnimarket.nodes.node_delegation_eval_orchestrator.sources.source_tenant_event_snapshot import (
    SnapshotNotFoundError,
)
from omnimarket.nodes.node_delegation_eval_run_orchestrator.handlers.handler_delegation_eval_run import (
    HandlerDelegationEvalRun,
)
from omnimarket.nodes.node_delegation_eval_run_orchestrator.handlers.handler_delegation_eval_run_runtime import (
    HandlerDelegationEvalRunRuntime,
)
from omnimarket.nodes.node_delegation_gate_eval_compute.handlers.handler_delegation_gate_eval import (
    HandlerDelegationGateEval,
)
from omnimarket.projection.tenant_isolation import TENANT_GUC

pytestmark = pytest.mark.unit

TENANT_A = "11111111-1111-1111-1111-111111111111"
TENANT_B = "22222222-2222-2222-2222-222222222222"
_NODES = Path(__file__).resolve().parents[1] / "src/omnimarket/nodes"
_DSN_ENV = "EVAL_SOURCE_READ_DSN_FOR_TEST"


def _item_row(key: str) -> dict[str, object]:
    return {
        "item_key": key,
        "task_class": "summarization",
        "stratum": "summarization/accepted",
        "label": "adequate",
        "prompt_snapshot": "Summarize the supplied facts.",
        "response_snapshot": "The supplied facts agree.",
        "gate_verdict": "accepted",
        "deciding_check": None,
    }


class _TenantStore:
    """In-memory stand-in for the two RLS tables: a read sees only the bound tenant's rows."""

    def __init__(self) -> None:
        self.reads: list[tuple[str, str, tuple[object, ...]]] = []
        self.eval_items = {TENANT_A: [_item_row("call-1:0"), _item_row("call-0:0")]}
        self.events = {
            TENANT_A: {
                "call-a": {
                    "correlation_id": "call-a",
                    "prompt_text": "prompt",
                    "response_text": "response",
                    "task_type": "summarization",
                    "quality_gate_passed": True,
                    "quality_gate_detail": "ok",
                    "quality_gates_failed_jsonb": [],
                }
            }
        }

    def reader(self, dsn: str, tenant_id: str) -> Any:
        store = self

        class _Reader:
            def fetch(
                self, table: str, sql: str, params: tuple[object, ...]
            ) -> list[dict[str, object]]:
                store.reads.append((table, tenant_id, params))
                if table == "delegation_eval_items":
                    return list(store.eval_items.get(tenant_id, []))
                return [
                    row
                    for key, row in store.events.get(tenant_id, {}).items()
                    if key == params[1]
                ]

        return _Reader()


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _TenantStore:
    root = tmp_path / "overlays" / binding.OVERLAY_ID
    root.mkdir(parents=True)
    (root / "overlay.yaml").write_text(f"read_dsn_env: {_DSN_ENV}\n")
    monkeypatch.setenv(OVERLAY_ROOTS_ENV, str(tmp_path / "overlays"))
    monkeypatch.setenv(_DSN_ENV, "postgresql://reader@lab.invalid/db")
    fake = _TenantStore()
    monkeypatch.setattr(binding, "TenantScopedRowReader", fake.reader)
    return fake


def _run_request(tenant: str = TENANT_A) -> ModelDelegationEvalRunRequest:
    return ModelDelegationEvalRunRequest(
        tenant_id=tenant,
        manifest_id="manifest-1",
        rater_role="human",
        rubric_version="v1",
        gate_version="gate-1",
    )


def _accepting_runtime() -> HandlerDelegationEvalRunRuntime:
    handler = HandlerDelegationEvalRunRuntime()
    handler._gate_eval = HandlerDelegationGateEval(
        Mock(return_value=ModelGateReplayVerdict(verdict="accepted"))
    )
    return handler


def test_runtime_handler_is_constructed_with_no_arguments_and_reads_the_calling_tenant(
    store: _TenantStore,
) -> None:
    payload = _accepting_runtime().handle(_run_request()).payload

    assert payload.status == "completed"
    assert [row.item_key for row in payload.item_verdicts] == ["call-0:0", "call-1:0"]
    table, tenant, params = store.reads[0]
    assert (table, tenant) == ("delegation_eval_items", TENANT_A)
    assert params == (TENANT_A, "human", "v1", "manifest-1")


def test_another_tenants_command_reads_none_of_the_first_tenants_rows(
    store: _TenantStore,
) -> None:
    payload = _accepting_runtime().handle(_run_request(TENANT_B)).payload

    assert payload.status == "failed"
    assert payload.item_verdicts == ()
    assert all(tenant == TENANT_B for _, tenant, _ in store.reads)


def test_unbound_handler_still_refuses_with_the_missing_source_error() -> None:
    with pytest.raises(RuntimeError, match="labelled-item source is required"):
        HandlerDelegationEvalRun().handle(_run_request())


def test_run_without_overlay_ends_in_a_failed_terminal(
    store: _TenantStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(OVERLAY_ROOTS_ENV)

    payload = _accepting_runtime().handle(_run_request()).payload

    assert payload.status == "failed"
    assert payload.failure_reasons == (
        "labelled-item source unavailable: NodeOverlayError",
    )
    assert store.reads == []


def test_run_with_the_named_dsn_variable_unset_ends_in_a_failed_terminal(
    store: _TenantStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(_DSN_ENV)

    payload = _accepting_runtime().handle(_run_request()).payload

    assert payload.status == "failed"
    assert payload.failure_reasons == (
        "labelled-item source unavailable: NodeOverlayError",
    )


def test_run_whose_read_fails_ends_in_a_failed_terminal_naming_only_the_error_type(
    store: _TenantStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Down:
        def fetch(self, table: str, sql: str, params: tuple[object, ...]) -> Any:
            raise ConnectionError("password=hunter2 host=lab.invalid")

    def _down_reader(*_args: object) -> _Down:
        return _Down()

    monkeypatch.setattr(binding, "TenantScopedRowReader", _down_reader)

    payload = _accepting_runtime().handle(_run_request()).payload

    assert payload.status == "failed"
    assert payload.failure_reasons == (
        "labelled-item source unavailable: ConnectionError",
    )
    assert "hunter2" not in repr(payload)


def _label_request(
    correlation_id: str, tenant: str = TENANT_A
) -> ModelLabelRecordRequest:
    return ModelLabelRecordRequest.model_validate(
        {
            "tenant_id": tenant,
            "correlation_id": correlation_id,
            "attempt_index": 0,
            "label": "adequate",
            "rater_role": "human",
            "rubric_version": "v1",
            "stratum": "summarization/accepted",
            "computed_facts": {"manifest_id": "manifest-1"},
        }
    )


def test_label_record_runtime_handler_snapshots_the_calling_tenants_event(
    store: _TenantStore,
) -> None:
    payload = (
        HandlerDelegationEvalOrchestratorRuntime()
        .handle(_label_request("call-a"))
        .payload
    )

    assert payload.prompt_snapshot == "prompt"
    assert payload.response_snapshot == "response"
    assert payload.gate_verdict == "accepted"
    assert store.reads == [("delegation_events", TENANT_A, (TENANT_A, "call-a"))]


def test_label_record_for_another_tenants_event_finds_nothing(
    store: _TenantStore,
) -> None:
    with pytest.raises(SnapshotNotFoundError):
        HandlerDelegationEvalOrchestratorRuntime().handle(
            _label_request("call-a", TENANT_B)
        )


@pytest.mark.parametrize(
    ("node", "handler_module", "handler_class"),
    [
        (
            "node_delegation_eval_run_orchestrator",
            "omnimarket.nodes.node_delegation_eval_run_orchestrator.handlers.handler_delegation_eval_run_runtime",
            "HandlerDelegationEvalRunRuntime",
        ),
        (
            "node_delegation_eval_orchestrator",
            "omnimarket.nodes.node_delegation_eval_orchestrator.handlers.handler_delegation_eval_orchestrator_runtime",
            "HandlerDelegationEvalOrchestratorRuntime",
        ),
    ],
)
def test_the_contract_routes_to_the_zero_argument_runtime_handler(
    node: str, handler_module: str, handler_class: str
) -> None:
    contract = yaml.safe_load((_NODES / node / "contract.yaml").read_text())
    routed = contract["handler_routing"]["handlers"][0]
    assert routed["handler"]["module"] == handler_module
    assert routed["handler"]["name"] == handler_class
    assert routed["handler_module"] == handler_module
    handler_type = getattr(importlib.import_module(handler_module), handler_class)
    assert handler_type()  # constructed as the runtime does, with no arguments


class _FakeCursor:
    def __init__(self, log: list[tuple[str, tuple[object, ...]]]) -> None:
        self._log = log

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, params: tuple[object, ...] = ()) -> None:
        self._log.append((sql, params))

    def fetchall(self) -> list[dict[str, object]]:
        return [{"item_key": "call-0:0"}]


def _connect_to(conn: _FakeConnection) -> Any:
    def connect(*_args: object, **_kwargs: object) -> _FakeConnection:
        return conn

    return connect


class _FakeConnection:
    def __init__(self) -> None:
        self.log: list[tuple[str, tuple[object, ...]]] = []
        self.session: dict[str, object] = {}
        self.events: list[str] = []

    def set_session(self, **kwargs: object) -> None:
        self.session = dict(kwargs)

    def cursor(self, **_: object) -> _FakeCursor:
        return _FakeCursor(self.log)

    def commit(self) -> None:
        self.events.append("commit")

    def rollback(self) -> None:
        self.events.append("rollback")

    def close(self) -> None:
        self.events.append("close")


def test_the_reader_sets_the_tenant_guc_before_its_statement_in_a_read_only_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _FakeConnection()
    monkeypatch.setattr(reader_module.psycopg2, "connect", _connect_to(conn))

    rows = reader_module.TenantScopedRowReader("postgresql://x", TENANT_A).fetch(
        "delegation_eval_items", "SELECT 1", ()
    )

    assert rows == [{"item_key": "call-0:0"}]
    assert conn.log[0] == ("SELECT set_config(%s, %s, true)", (TENANT_GUC, TENANT_A))
    assert conn.log[1][0] == "SELECT 1"
    assert conn.session == {"readonly": True, "autocommit": False}
    assert conn.events == ["commit", "close"]


def test_the_reader_rolls_back_and_closes_when_the_statement_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _FakeConnection()

    def _boom(self: _FakeCursor, sql: str, params: tuple[object, ...] = ()) -> None:
        if sql == "SELECT 1":
            raise RuntimeError("statement failed")

    monkeypatch.setattr(_FakeCursor, "execute", _boom)
    monkeypatch.setattr(reader_module.psycopg2, "connect", _connect_to(conn))

    with pytest.raises(RuntimeError, match="statement failed"):
        reader_module.TenantScopedRowReader("postgresql://x", TENANT_A).fetch(
            "delegation_eval_items", "SELECT 1", ()
        )

    assert conn.events == ["rollback", "close"]
