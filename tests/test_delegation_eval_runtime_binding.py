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
from omnimarket.handlers.node_overlay_reader import (
    OVERLAY_ROOTS_ENV,
    NodeOverlayError,
)
from omnimarket.nodes.node_delegation_eval_orchestrator.handlers.handler_delegation_eval_orchestrator import (
    HandlerDelegationEvalOrchestrator,
)
from omnimarket.nodes.node_delegation_eval_orchestrator.sources.source_tenant_event_snapshot import (
    SnapshotNotFoundError,
)
from omnimarket.nodes.node_delegation_eval_run_orchestrator.handlers.handler_delegation_eval_run import (
    HandlerDelegationEvalRun,
)
from omnimarket.nodes.node_delegation_gate_eval_compute.handlers.handler_delegation_gate_eval import (
    HandlerDelegationGateEval,
)
from omnimarket.projection.postgres_read_database import PostgresReadDatabaseAdapter

pytestmark = pytest.mark.unit

TENANT_A = "11111111-1111-1111-1111-111111111111"
TENANT_B = "22222222-2222-2222-2222-222222222222"
_NODES = Path(__file__).resolve().parents[1] / "src/omnimarket/nodes"
_DSN_ENV = "EVAL_SOURCE_READ_DSN_FOR_TEST"


def _item_row(key: str, manifest: str = "manifest-1") -> dict[str, object]:
    return {
        "computed_facts": {"manifest_id": manifest},
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
        self.reads: list[tuple[str, str, dict[str, object] | None]] = []
        self.closed = 0
        self.eval_items = {
            TENANT_A: [
                _item_row("call-1:0"),
                _item_row("call-0:0"),
                _item_row("other:0", manifest="manifest-2"),
            ]
        }
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

    def adapter(self, dsn: str, *, tenant_id: str) -> Any:
        store = self

        class _Reader:
            def query(
                self,
                table: str,
                filters: dict[str, object] | None = None,
                **_: object,
            ) -> list[dict[str, object]]:
                store.reads.append((table, tenant_id, filters))
                if table == "delegation_eval_items":
                    return list(store.eval_items.get(tenant_id, []))
                return [
                    row
                    for key, row in store.events.get(tenant_id, {}).items()
                    if key == (filters or {}).get("correlation_id")
                ]

            def close(self) -> None:
                store.closed += 1

        return _Reader()


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _TenantStore:
    root = tmp_path / "overlays" / binding.OVERLAY_ID
    root.mkdir(parents=True)
    (root / "overlay.yaml").write_text(f"read_dsn_env: {_DSN_ENV}\n")
    monkeypatch.setenv(OVERLAY_ROOTS_ENV, str(tmp_path / "overlays"))
    monkeypatch.setenv(_DSN_ENV, "postgresql://reader@lab.invalid/db")
    fake = _TenantStore()
    monkeypatch.setattr(binding, "PostgresReadDatabaseAdapter", fake.adapter)
    return fake


def _run_request(tenant: str = TENANT_A) -> ModelDelegationEvalRunRequest:
    return ModelDelegationEvalRunRequest(
        tenant_id=tenant,
        manifest_id="manifest-1",
        rater_role="human",
        rubric_version="v1",
        gate_version="gate-1",
    )


def _accepting_runtime() -> HandlerDelegationEvalRun:
    """Constructed with no source and no factory, as the runtime constructs it."""
    handler = HandlerDelegationEvalRun()
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
    table, tenant, filters = store.reads[0]
    assert (table, tenant) == ("delegation_eval_items", TENANT_A)
    assert filters == {
        "tenant_id": TENANT_A,
        "rater_role": "human",
        "rubric_version": "v1",
    }
    assert store.closed == 1


def test_another_tenants_command_reads_none_of_the_first_tenants_rows(
    store: _TenantStore,
) -> None:
    payload = _accepting_runtime().handle(_run_request(TENANT_B)).payload

    assert payload.status == "failed"
    assert payload.item_verdicts == ()
    assert all(tenant == TENANT_B for _, tenant, _ in store.reads)


def test_the_binding_builds_the_rls_seamed_adapter_for_the_calling_tenant(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(_DSN_ENV, "postgresql://reader@lab.invalid/db")
    environ = {
        _DSN_ENV: "postgresql://reader@lab.invalid/db",
        "ONEX_DELEGATION_EVAL_SOURCES_OVERLAY": "unused",
        OVERLAY_ROOTS_ENV: str(tmp_path),
    }
    (tmp_path / binding.OVERLAY_ID).mkdir()
    (tmp_path / binding.OVERLAY_ID / "overlay.yaml").write_text(
        f"read_dsn_env: {_DSN_ENV}\n"
    )

    reader = binding.build_tenant_row_query(TENANT_B, environ)

    assert isinstance(reader, PostgresReadDatabaseAdapter)
    assert reader._tenant_id == TENANT_B


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
        def query(self, table: str, filters: object = None, **_: object) -> Any:
            raise ConnectionError("password=hunter2 host=lab.invalid")

        def close(self) -> None:
            return None

    def _down_reader(*_args: object, **_kwargs: object) -> _Down:
        return _Down()

    monkeypatch.setattr(binding, "PostgresReadDatabaseAdapter", _down_reader)

    payload = _accepting_runtime().handle(_run_request()).payload

    assert payload.status == "failed"
    assert payload.failure_reasons == (
        "labelled-item source unavailable: ConnectionError",
    )
    assert "hunter2" not in repr(payload)


def test_an_injected_source_takes_precedence_over_the_overlay_binding(
    store: _TenantStore,
) -> None:
    source = Mock()
    source.get_labelled_items.return_value = ()

    payload = HandlerDelegationEvalRun(source).handle(_run_request()).payload

    assert payload.status == "failed"
    source.get_labelled_items.assert_called_once_with("manifest-1", "human", "v1")
    assert store.reads == []


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


def test_label_record_handler_snapshots_the_calling_tenants_event(
    store: _TenantStore,
) -> None:
    payload = (
        HandlerDelegationEvalOrchestrator().handle(_label_request("call-a")).payload
    )

    assert payload.prompt_snapshot == "prompt"
    assert payload.response_snapshot == "response"
    assert payload.gate_verdict == "accepted"
    assert store.reads == [
        (
            "delegation_events",
            TENANT_A,
            {"tenant_id": TENANT_A, "correlation_id": "call-a"},
        )
    ]
    assert store.closed == 1


def test_label_record_for_another_tenants_event_finds_nothing(
    store: _TenantStore,
) -> None:
    with pytest.raises(SnapshotNotFoundError):
        HandlerDelegationEvalOrchestrator().handle(_label_request("call-a", TENANT_B))


def test_label_record_without_overlay_raises_the_overlay_error(
    store: _TenantStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(OVERLAY_ROOTS_ENV)

    with pytest.raises(NodeOverlayError):
        HandlerDelegationEvalOrchestrator().handle(_label_request("call-a"))


@pytest.mark.parametrize(
    ("node", "handler_module", "handler_class"),
    [
        (
            "node_delegation_eval_run_orchestrator",
            "omnimarket.nodes.node_delegation_eval_run_orchestrator.handlers.handler_delegation_eval_run",
            "HandlerDelegationEvalRun",
        ),
        (
            "node_delegation_eval_orchestrator",
            "omnimarket.nodes.node_delegation_eval_orchestrator.handlers.handler_delegation_eval_orchestrator",
            "HandlerDelegationEvalOrchestrator",
        ),
    ],
)
def test_the_contract_routes_to_a_handler_the_runtime_builds_with_no_arguments(
    node: str, handler_module: str, handler_class: str
) -> None:
    contract = yaml.safe_load((_NODES / node / "contract.yaml").read_text())
    routed = contract["handler_routing"]["handlers"][0]
    assert routed["handler"]["module"] == handler_module
    assert routed["handler"]["name"] == handler_class
    handler_type = getattr(importlib.import_module(handler_module), handler_class)
    assert handler_type()  # constructed as the runtime does, with no arguments
