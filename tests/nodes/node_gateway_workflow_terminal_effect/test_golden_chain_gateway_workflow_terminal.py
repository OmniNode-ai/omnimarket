# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Failure-first tests of the gateway workflow terminal writer (OMN-26464)."""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import yaml

from omnimarket.nodes.node_gateway_workflow_terminal_effect.handlers import (
    handler_gateway_workflow_terminal as handler_module,
)
from omnimarket.nodes.node_gateway_workflow_terminal_effect.handlers import (
    wiring_overlay as wiring_module,
)
from omnimarket.nodes.node_gateway_workflow_terminal_effect.handlers.handler_gateway_workflow_terminal import (
    HandlerGatewayWorkflowTerminal,
    load_contract_routes,
)
from omnimarket.nodes.node_gateway_workflow_terminal_effect.models.model_delegation_terminal_envelope import (
    ModelDelegationTerminalEnvelope,
)
from omnimarket.nodes.node_gateway_workflow_terminal_effect.models.model_gateway_terminal_applied import (
    EnumGatewayTerminalDeadLetterReason as Reason,
)
from omnimarket.nodes.node_gateway_workflow_terminal_effect.models.model_gateway_terminal_applied import (
    EnumGatewayTerminalOutcome as Outcome,
)
from omnimarket.nodes.node_gateway_workflow_terminal_effect.protocols.protocol_gateway_terminal_store import (
    GatewayTerminalPublishError,
    GatewayTerminalStoreError,
)
from tests.nodes.node_gateway_workflow_terminal_effect.fakes import (
    FakeDeadLetter,
    FakeRow,
    FakeTerminalStore,
)

_NODE = Path(handler_module.__file__).resolve().parents[1]
_ROUTES, _DLQ = load_contract_routes()
_COMPLETED = next(t for t, s in _ROUTES.items() if s == "completed")
_FAILED = next(t for t, s in _ROUTES.items() if s == "failed")
_TENANT = uuid4()
_CORR = uuid4()


def _payload(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "correlation_id": str(_CORR),
        "tenant_id": str(_TENANT),
        "model_used": "glm-5.3-flash",
        "route": "byok-glm-general",
        "provider": "glm",
        "credential_source": "customer_key",
        "content": "The sky is blue.",
        "latency_ms": 7389,
        "total_tokens": 298,
        "failure_reason": "",
        "terminal_failure_reason": None,
        "rule_evaluations": [{"rule": "no_refusal", "passed": True}],
    }
    base.update(overrides)
    return {k: v for k, v in base.items() if v is not _DROP}


_DROP = object()


def _env(topic: str = _COMPLETED, **overrides: Any) -> ModelDelegationTerminalEnvelope:
    return ModelDelegationTerminalEnvelope(topic=topic, payload=_payload(**overrides))


def _make(
    rows: list[FakeRow] | None = None,
    *,
    store_fail: bool = False,
    dlq_fail: bool = False,
) -> tuple[HandlerGatewayWorkflowTerminal, FakeTerminalStore, FakeDeadLetter]:
    store = FakeTerminalStore(
        rows if rows is not None else [FakeRow(_TENANT, _CORR)], fail=store_fail
    )
    dlq = FakeDeadLetter(fail=dlq_fail)
    return HandlerGatewayWorkflowTerminal(store, dlq), store, dlq


async def test_completed_terminal_writes_all_fields() -> None:
    handler, store, dlq = _make()
    result = await handler.handle(_env())
    row = store.rows[0]
    assert result.outcome is Outcome.APPLIED
    assert result.workflow_id == row.workflow_id
    assert row.status == "completed"
    assert row.columns["model_used"] == "glm-5.3-flash"
    assert row.columns["total_tokens"] == 298
    assert row.columns["latency_ms"] == 7389
    assert (row.columns["route"], row.columns["provider"]) == (
        "byok-glm-general",
        "glm",
    )
    assert row.columns["credential_source"] == "customer_key"
    assert row.columns["content"] == "The sky is blue."
    assert row.columns["failure_reason"] is None  # empty wire string is NULL
    assert row.columns["rule_evaluations"] == [{"rule": "no_refusal", "passed": True}]
    assert dlq.records == []


async def test_failed_terminal_writes_failure_fields() -> None:
    handler, store, _ = _make()
    await handler.handle(
        _env(
            _FAILED,
            failure_reason="quality bar missed",
            terminal_failure_reason="CustomerKeyRefusedError: CODE_X",
        )
    )
    row = store.rows[0]
    assert row.status == "failed"
    assert row.columns["failure_reason"] == "quality bar missed"
    assert (row.columns["failure_class"], row.columns["failure_code"]) == (
        "CustomerKeyRefusedError",
        "CODE_X",
    )
    assert row.columns["model_used"] == "glm-5.3-flash"


async def test_failed_without_machine_readable_reason() -> None:
    handler, store, _ = _make()
    await handler.handle(_env(_FAILED, failure_reason="boom"))
    assert store.rows[0].columns["failure_class"] is None
    assert store.rows[0].columns["failure_code"] is None


async def test_failed_before_inference_producer_shape() -> None:
    """The failed terminal captured on dev for a run that failed before any inference."""
    handler, store, _ = _make()
    wire = {
        "correlation_id": str(_CORR),
        "task_type": "document",
        "model_used": "none",
        "endpoint_url": "none",
        "content": "",
        "operational_outcome": "boundary_failure",
        "latency_ms": 1398,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "failure_reason": "[REDACTED - potentially sensitive data]",
        "terminal_failure_reason": "ProtocolConfigurationError: ONEX_CORE_095_HANDLER_EXECUTION_ERROR",
        "tenant_id": str(_TENANT),
    }
    result = await handler.handle(
        ModelDelegationTerminalEnvelope(topic=_FAILED, payload=wire)
    )
    row = store.rows[0]
    assert result.outcome is Outcome.APPLIED
    assert row.status == "failed"
    assert row.columns["model_used"] == "none"
    assert row.columns["total_tokens"] == 0
    assert row.columns["route"] is None
    assert row.columns["provider"] is None
    assert row.columns["failure_class"] == "ProtocolConfigurationError"
    assert row.columns["failure_code"] == "ONEX_CORE_095_HANDLER_EXECUTION_ERROR"
    assert row.columns["rule_evaluations"] == []


@pytest.mark.parametrize("bad", [_DROP, "not-a-uuid", 7])
async def test_malformed_payload_is_dead_lettered(bad: Any) -> None:
    handler, store, dlq = _make()
    result = await handler.handle(_env(correlation_id=bad))
    assert result.outcome is Outcome.DEAD_LETTERED
    assert result.dead_letter_reason is Reason.MALFORMED_PAYLOAD
    assert store.calls == 0
    assert store.rows[0].status == "published"
    assert len(dlq.records) == 1
    assert dlq.records[0][0] == _DLQ


_MALFORMED_DLQ_TOPIC = "onex.dlq.omnimarket.gateway-workflow-terminal-malformed.v1"


async def test_malformed_terminal_is_routed_to_the_declared_malformed_dead_letter_topic() -> (
    None
):
    contract = yaml.safe_load((_NODE / "contract.yaml").read_text("utf-8"))
    handler, store, dlq = _make()
    result = await handler.handle(_env(correlation_id="not-a-uuid"))
    assert result.outcome is Outcome.DEAD_LETTERED
    assert result.dead_letter_reason is Reason.MALFORMED_PAYLOAD
    assert store.calls == 0
    assert store.rows[0].status == "published"
    ((topic, _key, record),) = dlq.records
    assert topic == _DLQ
    assert topic == _MALFORMED_DLQ_TOPIC
    assert topic in contract["event_bus"]["dlq_topics"]
    assert record["reason"] == Reason.MALFORMED_PAYLOAD.value
    assert "not-a-uuid" not in repr(record)


@pytest.mark.parametrize("model", [_DROP, "", "   ", 5])
async def test_missing_model_name_is_dead_lettered(model: Any) -> None:
    handler, store, dlq = _make()
    result = await handler.handle(_env(model_used=model))
    assert result.dead_letter_reason is Reason.MISSING_MODEL_USED
    assert store.calls == 0
    assert len(dlq.records) == 1


async def test_out_of_vocabulary_credential_source_is_dead_lettered() -> None:
    handler, store, dlq = _make()
    result = await handler.handle(_env(credential_source="other"))
    assert result.dead_letter_reason is Reason.MALFORMED_PAYLOAD
    assert store.calls == 0
    assert len(dlq.records) == 1


async def test_non_array_rule_evaluations_written_empty() -> None:
    handler, store, _ = _make()
    result = await handler.handle(_env(rule_evaluations="oops"))
    assert result.outcome is Outcome.APPLIED
    assert store.rows[0].columns["rule_evaluations"] == []
    assert handler.counters["terminal_rule_evaluations_dropped"] == 1


@pytest.mark.parametrize(
    "overrides",
    [
        {"total_tokens": -1},
        {"latency_ms": True},
        {"latency_ms": 2**40},
        {"total_tokens": _DROP},
        {"provider": _DROP},
        {"route": _DROP},
    ],
)
async def test_bad_metrics_are_dead_lettered(overrides: dict[str, Any]) -> None:
    handler, store, dlq = _make()
    result = await handler.handle(_env(**overrides))
    assert result.dead_letter_reason is Reason.MALFORMED_PAYLOAD
    assert store.calls == 0
    assert len(dlq.records) == 1


async def test_non_gateway_terminal_is_noop() -> None:
    handler, _store, dlq = _make(rows=[])
    result = await handler.handle(_env())
    assert result.outcome is Outcome.NOT_A_GATEWAY_WORKFLOW
    assert dlq.records == []
    assert handler.counters["not_a_gateway_workflow"] == 1


async def test_ambiguous_correlation_is_dead_lettered() -> None:
    rows = [FakeRow(_TENANT, _CORR), FakeRow(_TENANT, _CORR)]
    handler, _store, dlq = _make(rows)
    result = await handler.handle(_env())
    assert result.dead_letter_reason is Reason.AMBIGUOUS_CORRELATION
    assert [r.status for r in rows] == ["published", "published"]
    assert len(dlq.records) == 1


async def test_mixed_status_duplicates_are_ambiguous() -> None:
    rows = [FakeRow(_TENANT, _CORR, status="completed"), FakeRow(_TENANT, _CORR)]
    handler, _store, _dlq = _make(rows)
    result = await handler.handle(_env())
    assert result.dead_letter_reason is Reason.AMBIGUOUS_CORRELATION
    assert [r.status for r in rows] == ["completed", "published"]


async def test_cross_tenant_collision_updates_only_own_row() -> None:
    other = FakeRow(uuid4(), _CORR)
    mine = FakeRow(_TENANT, _CORR)
    handler, _, _ = _make([other, mine])
    await handler.handle(_env())
    assert mine.status == "completed"
    assert other.status == "published"


async def test_missing_tenant_is_unattributed_noop() -> None:
    handler, store, dlq = _make()
    result = await handler.handle(_env(tenant_id=_DROP))
    assert result.outcome is Outcome.NOT_A_GATEWAY_WORKFLOW
    assert store.calls == 0
    assert dlq.records == []
    assert handler.counters["terminal_unattributed"] == 1


async def test_invalid_tenant_id_is_dead_lettered() -> None:
    handler, store, dlq = _make()
    result = await handler.handle(_env(tenant_id="nope"))
    assert result.dead_letter_reason is Reason.MALFORMED_PAYLOAD
    assert store.calls == 0
    assert len(dlq.records) == 1


async def test_missing_model_with_bad_metrics() -> None:
    handler, _, _ = _make()
    result = await handler.handle(_env(model_used=" ", total_tokens=-4))
    assert result.dead_letter_reason is Reason.MISSING_MODEL_USED


async def test_dead_letter_precedence_and_metadata() -> None:
    handler, _, dlq = _make()
    secret = "SENTINEL-CONTENT-do-not-keep"
    result = await handler.handle(
        _env(
            correlation_id="raw-invalid-id",
            model_used=_DROP,
            total_tokens=-1,
            content=secret,
        )
    )
    assert result.dead_letter_reason is Reason.MALFORMED_PAYLOAD
    ((_, key, record),) = dlq.records
    blob = repr(record) + key
    assert secret not in blob
    assert "raw-invalid-id" not in blob
    assert "correlation_id" in record["fields_present"]
    assert "content" in record["fields_present"]
    assert set(record["fields_present"]) <= handler_module.WIRE_FIELDS
    assert "correlation_id" not in record
    assert record["tenant_id"] == str(_TENANT)


async def test_replayed_event_changes_nothing() -> None:
    handler, store, _ = _make()
    await handler.handle(_env())
    first = dict(store.rows[0].columns)
    result = await handler.handle(_env(model_used="other-model"))
    assert result.outcome is Outcome.ALREADY_TERMINAL
    assert store.rows[0].columns == first


async def test_failed_after_completed_changes_nothing() -> None:
    handler, store, _ = _make()
    await handler.handle(_env())
    result = await handler.handle(_env(_FAILED, failure_reason="late"))
    assert result.outcome is Outcome.ALREADY_TERMINAL
    assert store.rows[0].status == "completed"
    assert store.rows[0].columns["model_used"] == "glm-5.3-flash"


async def test_completed_after_failed_changes_nothing() -> None:
    handler, store, _ = _make()
    await handler.handle(_env(_FAILED, failure_reason="first"))
    result = await handler.handle(_env())
    assert result.outcome is Outcome.ALREADY_TERMINAL
    assert store.rows[0].status == "failed"


async def test_malformed_event_on_terminal_row_is_dead_lettered() -> None:
    handler, store, _dlq = _make([FakeRow(_TENANT, _CORR, status="completed")])
    result = await handler.handle(_env(model_used=_DROP))
    assert result.dead_letter_reason is Reason.MISSING_MODEL_USED
    assert store.calls == 0


@pytest.mark.parametrize("start", ["accepted", "published", "failed_publish"])
async def test_every_writable_starting_status_is_applied(start: str) -> None:
    handler, _store, _ = _make([FakeRow(_TENANT, _CORR, status=start)])
    assert (await handler.handle(_env())).outcome is Outcome.APPLIED


async def test_store_error_is_raised() -> None:
    handler, _, dlq = _make(store_fail=True)
    with pytest.raises(GatewayTerminalStoreError):
        await handler.handle(_env())
    assert dlq.records == []


async def test_dead_letter_publish_failure_is_raised() -> None:
    handler, _, _ = _make(dlq_fail=True)
    with pytest.raises(GatewayTerminalPublishError):
        await handler.handle(_env(model_used=_DROP))


async def test_unknown_topic_is_refused() -> None:
    handler, _, _ = _make()
    with pytest.raises(ValueError, match="not declared"):
        await handler.handle(
            ModelDelegationTerminalEnvelope(topic="x.y", payload=_payload())
        )


def test_contract_declares_topics_and_dead_letter() -> None:
    contract = yaml.safe_load((_NODE / "contract.yaml").read_text("utf-8"))
    declared = set(contract["event_bus"]["subscribe_topics"])
    assert declared == set(_ROUTES)
    assert len(declared) == 2
    assert _DLQ in contract["event_bus"]["dlq_topics"]
    assert _DLQ not in contract["event_bus"]["publish_topics"]
    table = contract["db_io"]["db_tables"][0]
    assert table["name"] == "gateway_workflows"
    assert table["access"] == "read_write"
    for module in (handler_module,):
        assert "onex.evt" not in inspect.getsource(module)
        assert "onex.dlq" not in inspect.getsource(module)


def test_handler_signature_is_canonical() -> None:
    params = inspect.signature(HandlerGatewayWorkflowTerminal.handle).parameters
    assert list(params) == ["self", "request"]


def test_uuid_helper_roundtrip() -> None:
    assert isinstance(_TENANT, UUID)


async def test_runtime_injected_flat_delivery_is_applied() -> None:
    """The runtime hands one flat dict: the wire fields beside _topic and other injected keys."""
    handler, store, _ = _make()
    flat = {**_payload(), "_topic": _COMPLETED, "_partition": 0, "_offset": 66301}
    request = ModelDelegationTerminalEnvelope.model_validate(flat)
    assert request.topic == _COMPLETED
    assert "_topic" not in request.payload
    assert (await handler.handle(request)).outcome is Outcome.APPLIED
    assert store.rows[0].status == "completed"


async def test_event_type_alone_selects_the_failed_status() -> None:
    handler, store, _ = _make()
    flat = {
        **_payload(failure_reason="boom"),
        "_event_type": "omnibase-infra.delegation-failed",
    }
    result = await handler.handle(ModelDelegationTerminalEnvelope.model_validate(flat))
    assert result.outcome is Outcome.APPLIED
    assert store.rows[0].status == "failed"


async def test_handler_without_arguments_builds_its_ports_from_the_overlay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, dlq = FakeTerminalStore([FakeRow(_TENANT, _CORR)]), FakeDeadLetter()
    monkeypatch.setattr(wiring_module, "build_ports_from_overlay", lambda: (store, dlq))
    handler = HandlerGatewayWorkflowTerminal()
    assert (await handler.handle(_env())).outcome is Outcome.APPLIED
    assert store.rows[0].status == "completed"
