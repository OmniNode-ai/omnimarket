# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain for node_secret_store_effect over the bus seam (OMN-20944).

The node is discovered from its contract, wired by the runtime's own dispatch
callback, driven by a command on the in-memory bus, and its terminal event is
read back from the contract's publish topic. The chain proves the bus never
carries a value: a resolve's terminal event says ``resolved`` with no value,
and a create sent as a command arrives without one and is refused.
"""

from __future__ import annotations

import importlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml
from pydantic import SecretStr

from omnimarket.nodes.node_secret_store_effect.adapters.handler_infisical_http import (
    HandlerInfisicalHttp,
)
from omnimarket.nodes.node_secret_store_effect.models import (
    EnumSecretStoreOperation,
    EnumSecretStoreOutcome,
    ModelSecretStoreOverlay,
    ModelSecretStoreRequest,
    ModelSecretStoreResult,
)
from tests.unit.nodes.node_secret_store_effect.test_handler_secret_store import (
    PLANTED,
    _FakeSecretStore,
)

pytestmark = pytest.mark.unit

_NODE_DIR = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_secret_store_effect"
)


def _overlay() -> ModelSecretStoreOverlay:
    return ModelSecretStoreOverlay(
        provider="fake",
        address="http://127.0.0.1:9",
        project_id="00000000-0000-0000-0000-000000000001",
        environment="dev",
        client_id_ref="id-ref",
        client_secret_ref="secret-ref",
    )


def _contract() -> dict[str, Any]:
    contract = yaml.safe_load((_NODE_DIR / "contract.yaml").read_text())
    assert contract["node_type"] == "effect"
    assert contract["terminal_event"] == "onex.evt.omnimarket.secret-store-completed.v1"
    assert (
        contract["runtime_dispatch"]["command_topic"]
        == "onex.cmd.omnimarket.secret-store-requested.v1"
    )
    assert contract["terminal_event"] in contract["event_bus"]["publish_topics"]
    return contract


def test_the_node_is_registered_and_its_handler_is_vendor_neutral() -> None:
    import tomllib

    root = _NODE_DIR.parents[3]
    registry = tomllib.loads((root / "pyproject.toml").read_text())["project"][
        "entry-points"
    ]["onex.nodes"]
    assert (
        registry["node_secret_store_effect"]
        == "omnimarket.nodes.node_secret_store_effect"
    )
    handler_source = (_NODE_DIR / "handlers" / "handler_secret_store.py").read_text()
    assert "infisical" not in handler_source.lower()


async def _run_over_bus(
    store: _FakeSecretStore | None,
    request: ModelSecretStoreRequest,
    *,
    overlay: ModelSecretStoreOverlay | None = None,
    bootstrap_store: _FakeSecretStore | None = None,
) -> tuple[ModelSecretStoreResult, list[bytes]]:
    from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
    from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
    from omnibase_infra.event_bus.models.model_event_message import ModelEventMessage
    from omnibase_infra.runtime.auto_wiring.discovery import _parse_contract
    from omnibase_infra.runtime.auto_wiring.handler_wiring import (
        _make_dispatch_callback,
    )
    from omnibase_infra.runtime.service_dispatch_result_applier import (
        DispatchResultApplier,
    )

    contract = _contract()
    declaration = contract["handler"]
    handler_type = getattr(
        importlib.import_module(declaration["module"]), declaration["class"]
    )
    handler = handler_type(
        store=store, overlay=overlay or _overlay(), bootstrap_store=bootstrap_store
    )
    discovered = _parse_contract(
        contract_path=_NODE_DIR / "contract.yaml",
        entry_point_name="node_secret_store_effect",
        package_name="omnimarket",
        package_version="0.0.0",
    )
    assert discovered.handler_routing is not None
    routes = {r.operation: r for r in discovered.handler_routing.handlers}
    assert set(routes) == {
        "resolve",
        "create",
        "list",
        "resolve_house_model_key",
        "generate_key",
    }
    route = routes[request.operation.value]
    assert route.event_model is not None
    callback = _make_dispatch_callback(handler, event_model=route.event_model)

    bus = EventBusInmemory(environment="test", group="secret-store")
    received: list[ModelSecretStoreResult] = []
    raw: list[bytes] = []
    applier = DispatchResultApplier(
        event_bus=bus,
        output_topic=contract["terminal_event"],
        allowed_output_topics=contract["event_bus"]["publish_topics"],
    )

    async def on_terminal(message: ModelEventMessage) -> None:
        raw.append(message.value)
        envelope = ModelEventEnvelope[ModelSecretStoreResult].model_validate_json(
            message.value
        )
        received.append(envelope.payload)

    async def on_command(message: ModelEventMessage) -> None:
        raw.append(message.value)
        await applier.apply(
            await callback(
                ModelEventEnvelope[object].model_validate_json(message.value)
            )
        )

    await bus.start()
    try:
        await bus.subscribe(
            contract["terminal_event"],
            group_id="secret-store-result",
            on_message=on_terminal,
        )
        await bus.subscribe(
            contract["runtime_dispatch"]["command_topic"],
            group_id="secret-store-command",
            on_message=on_command,
        )
        envelope = ModelEventEnvelope(
            payload=request,
            correlation_id=request.correlation_id,
            envelope_timestamp=datetime.now(UTC),
            event_type=request.operation.value,
            source_tool="secret-store-golden-chain",
        )
        await bus.publish(
            contract["runtime_dispatch"]["command_topic"],
            None,
            envelope.model_dump_json().encode(),
            None,
        )
    finally:
        await bus.close()
    (terminal,) = received
    return terminal, raw


async def test_golden_resolve_over_the_bus_reports_resolved_without_the_value() -> None:
    store = _FakeSecretStore({"/lab/TEST_KEY": PLANTED})
    request = ModelSecretStoreRequest(
        operation=EnumSecretStoreOperation.RESOLVE,
        reference="secret://onex/lab/TEST_KEY",
        correlation_id=uuid4(),
    )
    terminal, raw = await _run_over_bus(store, request)
    assert terminal.outcome is EnumSecretStoreOutcome.RESOLVED
    assert terminal.correlation_id == request.correlation_id
    assert terminal.value is None
    assert all(PLANTED.encode() not in message for message in raw)


async def test_golden_list_over_the_bus_returns_key_names() -> None:
    store = _FakeSecretStore({"/lab/A": PLANTED, "/lab/B": PLANTED})
    terminal, raw = await _run_over_bus(
        store,
        ModelSecretStoreRequest(
            operation=EnumSecretStoreOperation.LIST,
            folder="/lab",
            correlation_id=uuid4(),
        ),
    )
    assert terminal.outcome is EnumSecretStoreOutcome.LISTED
    assert terminal.keys == ("A", "B")
    assert all(PLANTED.encode() not in message for message in raw)


async def test_error_create_sent_over_the_bus_arrives_without_a_value_and_is_refused() -> (
    None
):
    store = _FakeSecretStore()
    terminal, raw = await _run_over_bus(
        store,
        ModelSecretStoreRequest(
            operation=EnumSecretStoreOperation.CREATE,
            key="KEY",
            value=SecretStr(PLANTED),
            correlation_id=uuid4(),
        ),
    )
    assert terminal.outcome is EnumSecretStoreOutcome.VALUE_MISSING
    assert store.calls == []
    assert all(PLANTED.encode() not in message for message in raw)


# --- the transport rule (OMN-23193) -----------------------------------------


class _RecordingExchange:
    """An HTTP exchange that records every request and answers a login."""

    def __init__(self) -> None:
        self.urls: list[str] = []

    async def __call__(
        self, method: str, url: str, **kwargs: Any
    ) -> tuple[int, dict[str, Any]]:
        self.urls.append(url)
        return 200, {"accessToken": "t", "expiresIn": 300}


def _adapter(
    address: str, exchange: _RecordingExchange, *hosts: str
) -> HandlerInfisicalHttp:
    return HandlerInfisicalHttp(
        address=address,
        project_id="00000000-0000-0000-0000-000000000001",
        environment="dev",
        client_id=SecretStr("id"),
        client_secret=SecretStr("secret"),
        exchange=exchange,
        timeout_seconds=5.0,
        **({"plain_http_hosts": hosts} if hosts else {}),
    )


@pytest.mark.parametrize(
    "address",
    [
        "http://store.example.test:8880",
        "http://203.0.113.9:8880",
        "http://198.51.100.5:8880",
        "http://[2001:db8::1]:8880",
    ],
)
def test_plain_http_to_a_remote_host_is_refused_before_any_request(
    address: str,
) -> None:
    exchange = _RecordingExchange()
    with pytest.raises(ValueError, match="remote store needs https") as refused:
        _adapter(address, exchange)
    assert type(refused.value).__name__ == "PlainHttpRefusedError"
    assert exchange.urls == []


def test_plain_http_to_a_host_the_overlay_names_is_still_refused_for_other_hosts() -> (
    None
):
    exchange = _RecordingExchange()
    with pytest.raises(ValueError, match="remote store needs https"):
        _adapter(
            "http://198.51.100.5:8880", exchange, "203.0.113.0/24", "vault.example.test"
        )
    with pytest.raises(ValueError, match="remote store needs https"):
        _adapter(
            "http://store.example.test",
            exchange,
            "203.0.113.0/24",
            "vault.example.test",
        )
    assert exchange.urls == []


@pytest.mark.parametrize(
    ("address", "hosts"),
    [
        ("http://198.51.100.5:8880", ("198.51.100.0/24",)),
        ("http://198.51.100.3", ("198.51.100.3",)),
        ("http://Vault.Example.Test:8880", ("vault.example.test",)),
    ],
)
async def test_plain_http_to_a_host_the_overlay_names_works(
    address: str, hosts: tuple[str, ...]
) -> None:
    exchange = _RecordingExchange()
    assert await _adapter(address, exchange, *hosts).health_check() is True
    assert exchange.urls == [f"{address}/api/v1/auth/universal-auth/login"]


@pytest.mark.parametrize(
    "address",
    [
        "https://store.example.test",
        "https://203.0.113.9:8443",
        "http://127.0.0.1:8880",
        "http://localhost:8880",
        "http://[::1]:8880",
    ],
)
async def test_https_and_loopback_addresses_still_work(address: str) -> None:
    exchange = _RecordingExchange()
    assert await _adapter(address, exchange).health_check() is True
    assert exchange.urls == [f"{address}/api/v1/auth/universal-auth/login"]


def test_plain_http_hosts_that_are_not_hosts_are_rejected_by_the_overlay() -> None:
    for bad in (
        "http://vault.example.test",
        "vault.example.test:8880",
        "198.51.100.0/99",
        "",
    ):
        with pytest.raises(ValueError, match="plain_http_hosts"):
            ModelSecretStoreOverlay(provider="infisical", plain_http_hosts=(bad,))
    assert ModelSecretStoreOverlay(
        provider="infisical", plain_http_hosts=("198.51.100.0/24", "vault.example.test")
    ).plain_http_hosts == ("198.51.100.0/24", "vault.example.test")


async def test_error_plain_http_to_a_remote_host_over_the_bus_is_misconfigured_and_sends_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnimarket.nodes.node_secret_store_effect.handlers import handler_secret_store

    exchange = _RecordingExchange()
    monkeypatch.setattr(handler_secret_store, "http_exchange", exchange)
    overlay = ModelSecretStoreOverlay(
        provider="infisical",
        address="http://store.example.test:8880",
        project_id="00000000-0000-0000-0000-000000000001",
        environment="dev",
        client_id_ref="id-ref",
        client_secret_ref="secret-ref",
    )
    bootstrap = _FakeSecretStore({"id-ref": "cid", "secret-ref": PLANTED})
    terminal, raw = await _run_over_bus(
        None,
        ModelSecretStoreRequest(
            operation=EnumSecretStoreOperation.RESOLVE,
            folder="/lab",
            key="TEST_KEY",
            correlation_id=uuid4(),
        ),
        overlay=overlay,
        bootstrap_store=bootstrap,
    )
    assert terminal.outcome is EnumSecretStoreOutcome.MISCONFIGURED
    assert exchange.urls == []
    assert all(PLANTED.encode() not in message for message in raw)
