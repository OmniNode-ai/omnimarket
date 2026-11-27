# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""An in-memory dev gateway for the delegation check tests (OMN-26642): the routes a customer reaches, answered healthy."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from omnimarket.nodes.node_delegation_check_probe_effect.models.model_gateway_check import (
    ModelGatewayObservation,
)

GATEWAY = "http://gateway.test.example:4100"
TENANT = "test-tenant-a"


class FakeGateway:
    """An in-memory gateway answering the routes a customer reaches; ``override`` replaces one answer."""

    def __init__(self, url: str = GATEWAY, tenant: str = TENANT) -> None:
        self.gateway_url = url
        self.tenant_slug = tenant
        self.chosen_provider: str | None = "local"
        self.calls: list[tuple[str, str, str]] = []
        self.override: Callable[
            [str, str, str, Any], ModelGatewayObservation | None
        ] = lambda _method, _path, _auth, _body: None
        self._runs: dict[str, dict[str, Any]] = {}

    def call(
        self, method: str, path: str, *, auth: str, body: dict[str, Any] | None = None
    ) -> ModelGatewayObservation:
        self.calls.append((method, path, auth))
        replaced = self.override(method, path, auth, body)
        if replaced is not None:
            return replaced
        return self._answer(method, path, auth, body)

    def _answer(
        self, method: str, path: str, auth: str, body: dict[str, Any] | None
    ) -> ModelGatewayObservation:
        if path == "/health":
            return ModelGatewayObservation(status=200, body={"status": "ok"})
        if auth == "none":
            return ModelGatewayObservation(
                status=401, body={"code": "auth.credential.absent"}
            )
        if auth == "invalid":
            return ModelGatewayObservation(
                status=401, body={"code": "auth.credential.invalid"}
            )
        if path == "/v1/workflows/contracts":
            return ModelGatewayObservation(
                status=200,
                body={
                    "contracts": [
                        {"workflow_type": "delegate-skill", "version": "1"},
                        {"workflow_type": "ledger-append", "version": "1"},
                    ]
                },
            )
        if method == "POST" and path == "/v1/workflows":
            assert body is not None
            payload = body["payload"]
            source = payload.get("source")
            if source not in ("claude-code", "codex", "external-client"):
                # The real gateway refuses a payload the workflow contract rejects 400 and names the violated field.
                return ModelGatewayObservation(
                    status=400,
                    body={
                        "detail": "payload does not conform to the workflow contract",
                        "workflow_type": "delegate-skill",
                        "errors": [
                            {
                                "field": "source",
                                "message": "does not match the contract",
                            }
                        ],
                    },
                )
            workflow_id = f"00000000-0000-4000-8000-{len(self._runs):012d}"
            dying = payload["task_type"] == "omn19181_not_a_task_class"
            self._runs[workflow_id] = {"dying": dying}
            return ModelGatewayObservation(
                status=202, body={"workflow_id": workflow_id}
            )
        match = re.match(r"^/v1/workflows/([^/]+)/(status|receipt)", path)
        if match and match.group(1) in self._runs:
            workflow_id = match.group(1)
            dying = self._runs[workflow_id]["dying"]
            # One terminal record, so the status and the receipt agree on every field both declared models carry.
            common: dict[str, Any] = {
                "workflow_id": workflow_id,
                "correlation_id": "00000000-0000-4000-8000-0000000000c1",
                "workflow_type": "delegate-skill",
                "status": "failed" if dying else "completed",
                "terminal_model_used": "none" if dying else "served-model",
                "terminal_total_tokens": 0 if dying else 41,
                "terminal_latency_ms": 12 if dying else 1840,
                "terminal_failure_class": "TaskClassError" if dying else None,
                "terminal_failure_code": "ONEX_TASK_CLASS_UNKNOWN" if dying else None,
                "terminal_failure_reason": "not a task class" if dying else None,
                "terminal_remediation": None,
                "rule_evaluations": []
                if dying
                else [{"rule": "non_empty", "passed": True, "enforcement": "blocking"}],
            }
            if match.group(2) == "status":
                return ModelGatewayObservation(status=200, body=common)
            return ModelGatewayObservation(
                status=200,
                body={
                    **common,
                    "route": None if dying else "lab-local",
                    "provider": None if dying else "local",
                },
            )
        if path.startswith("/v1/workflows/") and path.endswith("/receipt"):
            # An unknown run asked with no runner identity: the request is refused as malformed before it is looked up.
            return ModelGatewayObservation(
                status=422,
                body={
                    "detail": [{"type": "missing", "loc": ["query", "runner_identity"]}]
                },
            )
        return ModelGatewayObservation(status=404, body={"code": "resource.not_found"})


class FakeClock:
    """A clock that moves one second per sleep, so a budget of five seconds is five polls and no real time."""

    def __init__(self) -> None:
        self.now = 0.0

    def sleep(self, _s: float) -> None:
        self.now += 1.0

    def monotonic(self) -> float:
        return self.now
