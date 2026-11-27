# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A delegation call on a customer's own key connects only to the provider it chose.

The customer-machine probe grades every external connect() of the keyed session, so
pricing the call must not open a connection to models.dev. The price list is read at
setup and by the refresh command and kept under the ONEX home; a call reads only that
copy, else the list shipped in the install.

These tests run the whole delegation call with the real pricing path restored (the
suite's autouse fixture empties it) and with every socket connect and getaddrinfo
blocked and recorded. The positive control drives the explicit refresh through the
same probe, so an empty record for the call cannot mean the probe never saw a connect.
"""

from __future__ import annotations

import http.server
import json
import socket
import threading
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from omnimarket.enums.enum_cost_basis import EnumCostBasis
from omnimarket.enums.enum_cost_source import EnumCostSource
from omnimarket.events.llm_delegation_call import ModelLlmDelegationCallRequest
from omnimarket.handlers import model_catalog
from omnimarket.nodes.node_llm_delegation_call_effect.handlers import (
    handler_llm_delegation_call as call_handler,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers import transport
from omnimarket.nodes.node_llm_delegation_call_effect.models.model_llm_delegation_call_result import (
    ModelLlmDelegationCallResult,
)

pytestmark = pytest.mark.unit

#: The production pricing read; the suite's autouse fixture empties it per test.
_PRODUCTION_PRICING_INDEX = model_catalog.pricing_models_dev_index

MODEL = "qwen/qwen-plus"


class _Probe:
    """Every socket connect and getaddrinfo, recorded; refused while ``blocked``."""

    def __init__(self) -> None:
        self.attempts: list[str] = []
        self.blocked = True


@pytest.fixture
def probe(monkeypatch: pytest.MonkeyPatch) -> _Probe:
    seen = _Probe()
    real_connect = socket.socket.connect
    real_getaddrinfo = socket.getaddrinfo

    def connect(self: socket.socket, address: Any) -> None:
        seen.attempts.append(f"connect {address!r}")
        if seen.blocked:
            raise OSError("network blocked for this test")
        real_connect(self, address)

    def getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        seen.attempts.append(f"getaddrinfo {host!r}")
        if seen.blocked:
            raise socket.gaierror("name resolution blocked for this test")
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    return seen


@pytest.fixture
def real_pricing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """The production pricing read over an empty ONEX home; returns that home."""
    monkeypatch.setattr(model_catalog, "_onex_home", lambda: tmp_path)
    monkeypatch.setattr(
        model_catalog, "pricing_models_dev_index", _PRODUCTION_PRICING_INDEX
    )
    return tmp_path


def _delegation_call(monkeypatch: pytest.MonkeyPatch) -> ModelLlmDelegationCallResult:
    """One keyed call to a provider whose own transport is replaced (no socket)."""

    def post(**_kwargs: object) -> transport.ModelTransportResponse:
        return transport.ModelTransportResponse(
            status_code=200,
            json_body={
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000},
            },
            latency_ms=1,
        )

    monkeypatch.setattr(transport, "post_chat_completion", post)
    monkeypatch.setattr(call_handler, "_is_endpoint_healthy", lambda *_a, **_k: True)
    monkeypatch.setattr(call_handler, "_get_served_model_ids", lambda *_a, **_k: None)
    return call_handler.HandlerLlmDelegationCall()(
        ModelLlmDelegationCallRequest(
            request_id="req-nofetch",
            correlation_id="corr-nofetch",
            causation_id="caus-nofetch",
            model_id=MODEL,
            endpoint_ref="http://localhost:8000",
            prompt="Explain a hash map.",
            prompt_hash="nofetch",
            task_type="document",
            model_tier="cheap_cloud",
            provider="byok-openrouter",
            timeout_seconds=60.0,
        )
    )


@pytest.fixture
def models_dev_on_loopback(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """models.dev stood in for by a loopback server, so a real connect can happen."""
    document = json.dumps(
        {
            "openrouter": {
                "models": {MODEL: {"id": MODEL, "cost": {"input": 1, "output": 2}}}
            }
        }
    ).encode()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(document)))
            self.end_headers()
            self.wfile.write(document)

        def log_message(self, *_args: object) -> None:
            return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/api.json"
    monkeypatch.setattr(model_catalog, "service_endpoint_url", lambda _ref: url)
    try:
        yield url
    finally:
        server.shutdown()
        server.server_close()


def test_a_delegation_call_prices_with_no_connect_and_no_name_lookup(
    probe: _Probe, real_pricing: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty ONEX home: the call prices from the shipped list, estimated, off the network."""
    result = _delegation_call(monkeypatch)
    assert result.success
    assert probe.attempts == [], f"the call touched the network: {probe.attempts}"
    shipped = model_catalog.shipped_pricing_index()
    price = shipped.models[("openrouter", MODEL)]["cost"]
    assert result.actual_cost_usd == Decimal(str(price["input"])) + Decimal(
        str(price["output"])
    )
    assert result.cost_basis is EnumCostBasis.CLOUD_API_COST
    assert result.cost_source is EnumCostSource.ESTIMATED
    assert result.price_source == "shipped price list"
    assert result.price_read_at == shipped.fetched_at


def test_the_projection_price_read_has_no_connect_either(
    probe: _Probe, real_pricing: Path
) -> None:
    found = model_catalog.models_dev_price_with_read_time("openrouter", MODEL)
    assert found is not None
    assert probe.attempts == []


def test_positive_control_the_explicit_refresh_connects_and_the_probe_sees_it(
    probe: _Probe, real_pricing: Path, models_dev_on_loopback: str
) -> None:
    """The probe is not blind: with the block lifted, the setup-time read connects,
    and the call afterwards prices from what that read kept, still with no connect."""
    probe.blocked = False
    index = model_catalog.load_models_dev(real_pricing, force=True)
    assert index.error is None
    assert any(a.startswith("getaddrinfo") for a in probe.attempts)
    assert any(a.startswith("connect") for a in probe.attempts)
    assert (real_pricing / "cache" / "models_dev.json").is_file()

    probe.attempts.clear()
    probe.blocked = True
    kept = model_catalog.pricing_models_dev_index()
    assert kept.source == "models.dev"
    assert probe.attempts == []


def test_a_model_absent_from_the_shipped_list_stays_unpriced_with_no_connect(
    probe: _Probe, real_pricing: Path
) -> None:
    """Negative control: an unlisted model is unpriced, and the call still never connects."""
    assert (
        model_catalog.models_dev_price_per_1m("openrouter", "no-such/model-zzz") is None
    )
    assert probe.attempts == []


def test_the_shipped_list_parses_and_prices_every_entry_with_two_numbers() -> None:
    index = model_catalog.shipped_pricing_index()
    assert len(index.models) > 100
    assert index.stale is False
    assert index.source == model_catalog.SHIPPED_PRICE_SOURCE
    for (_provider, _model), entry in index.models.items():
        cost = entry["cost"]
        assert cost["input"] >= 0
        assert cost["output"] >= 0
