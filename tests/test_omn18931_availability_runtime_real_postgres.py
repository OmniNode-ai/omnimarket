# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""K4: real HTTP failures, typed terminals, registered runtime and Postgres.

Run in-process on the lab so fault injection cannot spend or throttle a shared
provider. The loopback server supplies HTTP responses; it replaces neither the
inference effect nor the orchestrator nor the projection writer. This proves
the candidate seam, not E11's released-runtime/broker-offset closeout receipt.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from importlib.resources import files
from pathlib import Path
from uuid import uuid4

import pytest
from omnibase_core.enums.enum_workflow_result import EnumWorkflowResult
from omnibase_core.models.delegation.wire import (
    ModelDelegationResult,
    ModelInferenceIntent,
)
from omnibase_core.runtime.runtime_local import RuntimeLocal

from omnimarket.cost.usage_normalizer import normalize_usage
from omnimarket.inference.provider_quota_state import StaticProviderQuotaReader
from omnimarket.nodes.node_delegation_availability_report_compute.models.model_delegation_availability_report import (
    ModelDelegationAvailabilityReport,
)
from omnimarket.nodes.node_delegation_availability_report_compute.models.model_delegation_availability_request import (
    ModelDelegationAvailabilityRequest,
)
from omnimarket.nodes.node_delegation_availability_report_compute.models.model_delegation_cohort_observation import (
    ModelDelegationCohortObservation,
)
from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    HandlerDelegationWorkflow,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request import (
    ModelDelegationRequest,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_result import (
    ModelQualityGateResult,
)
from omnimarket.nodes.node_delegation_routing_reducer.models.model_routing_decision import (
    ModelRoutingDecision,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_inference_intent import (
    HandlerInferenceIntent,
)
from omnimarket.projection.runner import MessageMeta
from tests.test_omn15909_real_postgres_projection_write_path_gate import (
    _provisioned_runner,
)

pytestmark = pytest.mark.integration
_MODEL = "availability-fixture"
_TENANT = "beta-business-proof"
_STATUSES = (429, 200, 503)


def _body(status: int) -> dict[str, object]:
    if status != 200:
        return {"error": {"code": status, "message": f"HTTP {status} unavailable"}}
    return {
        "id": "availability-control",
        "model": _MODEL,
        "choices": [
            {"message": {"content": "### ANSWER\nOK"}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


@pytest.fixture
def provider() -> Iterator[tuple[str, list[int]]]:
    calls: list[int] = []

    class _Provider(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            """Keep the synthetic server's access log out of test output."""

        def do_GET(self) -> None:
            self._send(200, {"data": [{"id": _MODEL}]})

        def do_POST(self) -> None:
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert payload["model"] == _MODEL
            status = _STATUSES[len(calls)]
            calls.append(status)
            self._send(status, _body(status))

        def _send(self, status: int, body: dict[str, object]) -> None:
            wire = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(wire)))
            self.end_headers()
            self.wfile.write(wire)

    server = HTTPServer(("127.0.0.1", 0), _Provider)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1/chat/completions", calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


async def test_same_route_failures_and_later_control_keep_separate_denominators(
    provider: tuple[str, list[int]],
    tmp_path: Path,
) -> None:
    endpoint, calls = provider
    backend_id = uuid4()
    observations: list[ModelDelegationCohortObservation] = []
    terminals: list[ModelDelegationResult] = []
    for status in _STATUSES:
        cid = uuid4()
        handler = HandlerDelegationWorkflow(
            workflows={}, quota_reader=StaticProviderQuotaReader()
        )
        handler.handle_delegation_request(
            ModelDelegationRequest(
                prompt="Return OK.",
                task_type="document",
                correlation_id=cid,
                tenant_id=_TENANT,
                emitted_at=datetime.now(UTC),
            )
        )
        events = handler.handle_routing_decision(
            ModelRoutingDecision(
                correlation_id=cid,
                task_type="document",
                selected_model=_MODEL,
                selected_backend_id=backend_id,
                endpoint_url=endpoint,
                cost_tier="high",
                tier_name="claude",
                max_context_tokens=4096,
                max_tokens=64,
                system_prompt="Return OK.",
                rationale="Fixed cohort at the declared ceiling; no higher tier.",
                route="availability-fixture",
                provider="loopback",
            )
        )
        intent = next(
            event for event in events if isinstance(event, ModelInferenceIntent)
        )
        response = await asyncio.to_thread(HandlerInferenceIntent().handle, intent)
        events = handler.handle_inference_response(response)
        if status == 200:
            assert response.content == "### ANSWER\nOK"
            # K4 uses a known-valid control. Content acceptance policy is K5's
            # gate; this test does not replace or claim its output-only proof.
            events = handler.handle_gate_result(
                ModelQualityGateResult(correlation_id=cid, passed=True, quality_score=1)
            )
        terminal = next(
            event for event in events if isinstance(event, ModelDelegationResult)
        )
        assert terminal.attempts_count == 1
        assert terminal.escalation_count == 0
        assert terminal.model_used == _MODEL
        assert terminal.endpoint_url == endpoint
        assert terminal.cost_tier_name == "claude"
        assert terminal.route == "availability-fixture"
        assert terminal.provider == "loopback"
        terminals.append(terminal)
        body = json.dumps(_body(status)).encode()
        observations.append(
            ModelDelegationCohortObservation(
                correlation_id=cid,
                backend_tier="claude",
                attempts_count=1,
                terminal=terminal,
                usage=normalize_usage(_body(status), _MODEL),
                source_payload_hash=hashlib.sha256(body).hexdigest(),
            )
        )

    assert calls == list(_STATUSES), "exactly one HTTP attempt per independent request"
    assert [terminal.operational_outcome for terminal in terminals] == [
        "provider_quota",
        "completed",
        "provider_unavailable",
    ]
    async with _provisioned_runner() as (runner, connection, _schema):
        assert (await connection.fetchval("SHOW server_version_num")) >= "150000"
        for offset, terminal in enumerate(terminals):
            topic = (
                runner._topic_delegation_completed
                if offset == 1
                else runner._topic_delegation_failed
            )
            payload = terminal.model_dump(mode="json")
            payload["tenant_id"] = _TENANT
            assert await runner.project_event(
                topic,
                payload,
                MessageMeta(
                    partition=0, offset=offset, fallback_id=str(terminal.correlation_id)
                ),
            )
            row = await connection.fetchrow(
                "SELECT operational_outcome, content_verdict, actual_score, "
                "escalation_count FROM delegation_events WHERE correlation_id = $1",
                str(terminal.correlation_id),
            )
            assert row is not None
            assert row["operational_outcome"] == terminal.operational_outcome
            assert row["content_verdict"] == terminal.content_verdict
            assert row["escalation_count"] == 0
            assert row["actual_score"] == (1 if offset == 1 else None)

    contract_path = Path(
        str(
            files(
                "omnimarket.nodes.node_delegation_availability_report_compute"
            ).joinpath("contract.yaml")
        )
    )
    input_path = tmp_path / "cohort.json"
    input_path.write_text(
        ModelDelegationAvailabilityRequest(
            observations=tuple(observations)
        ).model_dump_json()
    )
    runtime = RuntimeLocal(
        workflow_path=contract_path,
        input_path=input_path,
        state_root=tmp_path / "state",
        timeout=5,
        backend_overrides={"event_bus": "inmemory"},
    )
    assert await runtime.run_async() is EnumWorkflowResult.COMPLETED
    assert runtime.handler_result is not None
    report = ModelDelegationAvailabilityReport.model_validate_json(
        runtime.handler_result.model_dump_json()
    )
    assert report.tiers[0].availability_total == 3
    assert report.tiers[0].availability_failures == 2
    assert report.tiers[0].correctness_total == 1
    assert report.rows[1].quality_score == 1
    for row in (report.rows[0], report.rows[2]):
        assert row.content_verdict is None
        assert row.quality_score is None
    assert [row.source_payload_hash for row in report.rows] == [
        observation.source_payload_hash for observation in observations
    ]
    assert report.excluded_requests == ()
