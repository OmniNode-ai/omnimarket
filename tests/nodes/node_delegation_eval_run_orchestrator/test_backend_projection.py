# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Backend pins survive replay and the real projection writer boundary."""

from datetime import UTC, datetime
from unittest.mock import Mock

from omnimarket.events.delegation_eval import ModelDelegationEvalRunRequest
from omnimarket.events.delegation_gate_eval.model_gate_eval_item import (
    ModelGateEvalItem,
)
from omnimarket.models.delegation_eval_backend import ModelDelegationEvalBackend
from omnimarket.nodes.node_delegation_eval_run_orchestrator.handlers import (
    HandlerDelegationEvalRun,
)
from omnimarket.nodes.node_projection_delegation_eval.handlers.handler_projection_delegation_eval_run import (
    HandlerProjectionDelegationEvalRun,
)
from omnimarket.nodes.node_projection_delegation_eval.models import (
    ModelDelegationEvalRunProjectionRequest,
)


def test_pin_survives_run_and_projection_and_changes_run_identity() -> None:
    backend = ModelDelegationEvalBackend(
        backend_id="q8",
        endpoint="http://host:8130",
        host="host",
        model="qwen",
        model_file="/q8.gguf",
        quant="Q8_0",
    )
    source = Mock()
    source.get_labelled_items.return_value = (
        ModelGateEvalItem(
            item_id="i",
            task_class="reasoning",
            stratum="all",
            label="unlabelable",
            prompt_text="Say hello",
            recorded_answer="Hello",
        ),
    )
    request = ModelDelegationEvalRunRequest(
        tenant_id="11111111-1111-1111-1111-111111111111",
        manifest_id="m",
        rater_role="unlabelled:fresh",
        rubric_version="v1",
        gate_version="gate",
        backend_configuration=backend,
        generation_sha256="a" * 64,
        item_latencies_ms={"i": 12.5},
    )
    handler = HandlerDelegationEvalRun(
        source, clock=lambda: datetime(2026, 10, 26, tzinfo=UTC)
    )
    payload = handler.handle(request).payload
    assert payload.item_verdicts[0].backend_configuration == backend
    assert payload.item_verdicts[0].latency_ms == 12.5
    assert payload.backend_configuration == backend
    projected = HandlerProjectionDelegationEvalRun().handle(
        ModelDelegationEvalRunProjectionRequest.model_validate(payload.model_dump())
    )
    assert projected.verdict_rows[0].backend_configuration == backend
    assert projected.result_rows[0].backend_configuration == backend
    assert handler.handle(request).payload.eval_run_id == payload.eval_run_id
    other = backend.model_copy(update={"backend_id": "q3", "quant": "Q3_K_M"})
    assert (
        handler.handle(
            request.model_copy(update={"backend_configuration": other})
        ).payload.eval_run_id
        != payload.eval_run_id
    )
    assert (
        handler.handle(
            request.model_copy(update={"generation_sha256": "b" * 64})
        ).payload.eval_run_id
        != payload.eval_run_id
    )
    assert all(r.false_pass_line_verdict == "refused" for r in payload.results)


def test_writer_serializes_backend_configuration() -> None:
    import json

    from omnimarket.nodes.node_projection_delegation_eval.handlers.handler_delegation_eval_writer import (
        DelegationEvalProjectionWriter,
    )
    from tests.test_golden_chain_projection_delegation_eval import (
        _gate_items,
        _run_payload,
        _RunStore,
    )

    event = _run_payload(_gate_items())
    event["backend_configuration"] = {
        "backend_id": "q8",
        "endpoint": "http://host:8130",
        "host": "host",
        "model": "qwen",
        "model_file": "/q8.gguf",
        "quant": "Q8_0",
    }
    for verdict in event["item_verdicts"]:
        verdict["backend_configuration"] = event["backend_configuration"]
        verdict["latency_ms"] = 7.0
    writer = DelegationEvalProjectionWriter()
    store = _RunStore()
    writer._db = store
    writer.handle(event)
    for row in store.tables["delegation_eval_item_verdicts"].values():
        assert json.loads(row["backend_configuration"])["quant"] == "Q8_0"
        assert row["latency_ms"] == 7.0
