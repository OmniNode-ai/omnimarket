# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC2 (Task 19): one wiring function builds a registered store kind from the bootstrap inputs that name it."""

from __future__ import annotations

from collections.abc import Callable, Mapping

import pytest
from omnibase_core.models.container.model_onex_container import ModelONEXContainer
from omnibase_spi.protocols.storage import ProtocolOverlayStore

from omnimarket.handlers.overlay_store.store_backend_registry import (
    StoreWiringRefusedError,
)
from omnimarket.handlers.overlay_store.store_in_memory import InMemoryOverlayStore
from omnimarket.handlers.overlay_store.store_wiring import (
    KIND_INPUT,
    build_overlay_store,
    wire_overlay_store,
)
from omnimarket.models.overlay_store.model_store_backend import ModelStoreBackendSpec


class _RecordingBackend:
    """A backend double: declares what it needs, counts what it built."""

    def __init__(self, spec: ModelStoreBackendSpec) -> None:
        self.spec = spec
        self.built: list[tuple[Mapping[str, str], Mapping[str, str]]] = []

    def build(
        self, inputs: Mapping[str, str], secrets: Mapping[str, str]
    ) -> ProtocolOverlayStore:
        self.built.append((dict(inputs), dict(secrets)))
        return InMemoryOverlayStore()


def _network_backend() -> _RecordingBackend:
    return _RecordingBackend(
        ModelStoreBackendSpec(
            kind="net_kind",
            inputs=("NET_ADDRESS", "NET_PORT"),
            secrets=("NET_PASSWORD",),
        )
    )


def _memory_backend() -> _RecordingBackend:
    return _RecordingBackend(
        ModelStoreBackendSpec(kind="memory_kind", inputs=(), secrets=())
    )


def _registry(*backends: _RecordingBackend) -> dict[str, _RecordingBackend]:
    return {b.spec.kind: b for b in backends}


def _no_secrets(names: tuple[str, ...]) -> Mapping[str, str]:
    raise AssertionError(f"Infisical was called for {names}")


def test_store_wiring_in_memory_kind_needs_no_other_input_and_no_infisical() -> None:
    backend = _memory_backend()
    store = build_overlay_store(
        {KIND_INPUT: "memory_kind"},
        backends=_registry(backend, _network_backend()),
        fetch_secrets=_no_secrets,
    )
    assert isinstance(store, InMemoryOverlayStore)
    assert len(backend.built) == 1


def test_store_wiring_real_in_memory_kind_builds_without_inputs_or_infisical() -> None:
    store = build_overlay_store(
        {KIND_INPUT: "store_in_memory"}, fetch_secrets=_no_secrets
    )
    assert isinstance(store, InMemoryOverlayStore)


def test_store_wiring_builds_a_kind_from_the_inputs_that_name_it() -> None:
    backend = _network_backend()
    fetched: list[tuple[str, ...]] = []

    def fetch(names: tuple[str, ...]) -> Mapping[str, str]:
        fetched.append(names)
        return {name: f"value-of-{name}" for name in names}

    build_overlay_store(
        {
            KIND_INPUT: "net_kind",
            "NET_ADDRESS": "a",
            "NET_PORT": "1",
            "UNRELATED_INPUT": "never passed on",
        },
        backends=_registry(backend, _memory_backend()),
        fetch_secrets=fetch,
    )
    assert fetched == [("NET_PASSWORD",)]
    inputs, secrets = backend.built[0]
    assert inputs == {"NET_ADDRESS": "a", "NET_PORT": "1"}
    assert secrets == {"NET_PASSWORD": "value-of-NET_PASSWORD"}


def test_store_wiring_refuses_a_missing_kind_by_name_and_builds_nothing() -> None:
    backend = _memory_backend()
    with pytest.raises(StoreWiringRefusedError, match=KIND_INPUT):
        build_overlay_store({}, backends=_registry(backend), fetch_secrets=_no_secrets)
    assert backend.built == []


@pytest.mark.parametrize("missing", ["NET_ADDRESS", "NET_PORT"])
def test_store_wiring_refuses_a_missing_declared_input_by_name_and_builds_nothing(
    missing: str,
) -> None:
    backend = _network_backend()
    inputs = {KIND_INPUT: "net_kind", "NET_ADDRESS": "a", "NET_PORT": "1"}
    del inputs[missing]
    with pytest.raises(StoreWiringRefusedError, match=missing):
        build_overlay_store(
            inputs, backends=_registry(backend), fetch_secrets=_no_secrets
        )
    assert backend.built == []


def test_store_wiring_refuses_an_empty_declared_input() -> None:
    backend = _network_backend()
    with pytest.raises(StoreWiringRefusedError, match="NET_PORT"):
        build_overlay_store(
            {KIND_INPUT: "net_kind", "NET_ADDRESS": "a", "NET_PORT": "  "},
            backends=_registry(backend),
            fetch_secrets=_no_secrets,
        )
    assert backend.built == []


def test_store_wiring_refuses_an_unregistered_kind_naming_it() -> None:
    with pytest.raises(StoreWiringRefusedError, match="nope_kind"):
        build_overlay_store(
            {KIND_INPUT: "nope_kind"},
            backends=_registry(_memory_backend()),
            fetch_secrets=_no_secrets,
        )


def test_store_wiring_refuses_a_secret_the_infisical_answer_lacks() -> None:
    backend = _network_backend()
    fetch: Callable[[tuple[str, ...]], Mapping[str, str]] = lambda _names: {}  # noqa: E731
    with pytest.raises(StoreWiringRefusedError, match="NET_PASSWORD"):
        build_overlay_store(
            {KIND_INPUT: "net_kind", "NET_ADDRESS": "a", "NET_PORT": "1"},
            backends=_registry(backend),
            fetch_secrets=fetch,
        )
    assert backend.built == []


def test_store_wiring_a_secret_kind_without_a_fetcher_refuses() -> None:
    backend = _network_backend()
    with pytest.raises(StoreWiringRefusedError, match="NET_PASSWORD"):
        build_overlay_store(
            {KIND_INPUT: "net_kind", "NET_ADDRESS": "a", "NET_PORT": "1"},
            backends=_registry(backend),
        )
    assert backend.built == []


async def test_store_wiring_registers_the_store_in_the_container_under_the_protocol() -> (
    None
):
    container = ModelONEXContainer()
    store = await wire_overlay_store(
        container,
        {KIND_INPUT: "memory_kind"},
        backends=_registry(_memory_backend()),
        fetch_secrets=_no_secrets,
    )
    resolved = await container.get_service_async(ProtocolOverlayStore)  # type: ignore[type-abstract]
    assert resolved is store
