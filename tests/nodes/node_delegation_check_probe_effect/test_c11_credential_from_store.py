# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""C11 reads the test tenant's credential from the secret store and sends only that credential (OMN-26117).

The gateway here is a real local HTTP server that accepts exactly one key: the one the secret store holds. C11 runs
through the real caller and the real tenant credential reader, so a green reading proves the key on the wire came from
the store. The known bad answers are a store holding another key (the tenant arms are refused) and a store holding
nothing (PRECONDITION-ABSENT, never green).
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest
from _gateway_fake import TENANT, FakeClock
from pydantic import SecretStr

from omnimarket.models.overlay_documents.model_delegation_check_gateway_overlay import (
    ModelDelegationCheckGatewayOverlay,
    ModelDevTenantTokenSource,
)
from omnimarket.nodes.node_delegation_check_probe_effect.handlers.adapter_gateway import (
    DevTenantCredential,
    HttpGatewayCaller,
)
from omnimarket.nodes.node_delegation_check_probe_effect.handlers.gateway_checks import (
    GatewayRunSettings,
    run_gateway_check,
)
from omnimarket.nodes.node_delegation_check_probe_effect.protocols.protocol_delegation_check import (
    CollectorAbsentError,
)

SECRET_NAME = "DEV_TENANT_SECRET_NAME"
STORED_KEY = "onxk_" + "a" * 32
OTHER_KEY = "onxk_" + "b" * 32


class _Store:
    def __init__(self, value: str | None) -> None:
        self.value = value
        self.reads: list[tuple[str, str]] = []

    async def read(self, folder: str, name: str) -> SecretStr | None:
        self.reads.append((folder, name))
        return SecretStr(self.value) if self.value is not None else None


def _source() -> ModelDevTenantTokenSource:
    return ModelDevTenantTokenSource.model_validate(
        {
            "store": {"provider": "infisical"},
            "token_folder": "/shared/env",
            "tenants": [
                {"tenant_slug": TENANT, "tenant_id": "x", "secret_name": SECRET_NAME}
            ],
        }
    )


def _gateway(accepts: str) -> HTTPServer:
    """A gateway that owns one tenant key; every tenant route answers as the C11 arms expect."""

    class _H(BaseHTTPRequestHandler):
        def _send(self, status: int, body: dict[str, Any]) -> None:
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())

        def do_GET(self) -> None:
            if self.path == "/health":
                return self._send(200, {"ok": True})
            key = self.headers.get("x-api-key")
            if key is None:
                return self._send(401, {"code": "auth.credential.absent"})
            if key != accepts:
                return self._send(401, {"code": "auth.credential.invalid"})
            if self.path.endswith("/receipt"):
                return self._send(
                    422,
                    {
                        "detail": [
                            {"type": "missing", "loc": ["query", "runner_identity"]}
                        ]
                    },
                )
            if self.path.endswith("/status"):
                return self._send(404, {"code": "resource.not_found"})
            return self._send(200, {"contracts": []})

        def log_message(self, *_a: object) -> None:
            return None

    server = HTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _c11(store_value: str | None) -> tuple[Any, _Store]:
    server = _gateway(STORED_KEY)
    store = _Store(store_value)
    try:
        row = ModelDelegationCheckGatewayOverlay.model_construct(
            schema_version=1,
            gateway_url=f"http://127.0.0.1:{server.server_address[1]}",
            tenant_slug=TENANT,
            credential_kind="api_key",
        )
        client = HttpGatewayCaller(row, DevTenantCredential(_source(), store), 5.0)
        clock = FakeClock()
        reading = run_gateway_check(
            "C11",
            client,
            GatewayRunSettings(run_budget_s=5.0, poll_interval_s=0.0),
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )
    finally:
        server.shutdown()
    return reading, store


@pytest.mark.unit
def test_c11_known_good_the_key_the_store_holds_grades_every_clause_pass() -> None:
    reading, store = _c11(STORED_KEY)
    assert [c.name for c in reading.clauses] == [
        "positive_control",
        "wrong_key",
        "absent_key",
        "wrong_tenant",
        "malformed",
    ]
    assert all(c.ok for c in reading.clauses), reading.clauses
    # The credential came from the store, by the name the dev tenant node declares.
    assert store.reads == [("/shared/env", SECRET_NAME)]


@pytest.mark.unit
def test_c11_known_bad_a_store_holding_another_key_fails_the_tenant_arms() -> None:
    reading, _ = _c11(OTHER_KEY)
    failed = {c.name for c in reading.clauses if not c.ok}
    assert failed == {"wrong_tenant", "malformed"}


@pytest.mark.unit
def test_c11_known_bad_a_store_holding_nothing_is_precondition_absent() -> None:
    with pytest.raises(CollectorAbsentError, match="not in the dev secret store"):
        _c11(None)
