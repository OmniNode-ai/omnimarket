# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Runtime routing of the dev signed-in read: the runtime's own loader, wiring and dispatch, on an in-memory bus.

The command is published on the declared command topic. The contract is read by the runtime's contract discovery
and wired by ``wire_from_manifest`` into a ``MessageDispatchEngine`` on an in-memory bus, the way the dev runtime
builds its ingress route: the handler is built with no arguments, dispatched by the runtime and its result reaches
the completed or failed topic. A typo in the declared event type or handler module therefore fails these tests.

How the runtime picks the handler: the dispatcher is indexed under the entry's ``event_type`` (and the event model
class name and the topic-derived alias), and the message type of an inbound command is the topic-derived alias
``omnimarket.dev-signed-in-read-requested``. The command payload carries no operation name (``lane`` and ``write``
only), so ``operation_match`` does not select among entries here: with one entry, the ``event_type`` is what makes the
runtime dispatch to it. The route declared at ``51bdb423ac65`` had ``event_type: omnimarket.dev-signed-in-read``,
which matches no inbound message, and the runtime answered the command with ``no_dispatcher``.
"""

from __future__ import annotations

import asyncio
import importlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, NoReturn
from uuid import uuid4

import pytest
import yaml
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_infra.runtime.auto_wiring import discover_contracts_from_paths
from omnibase_infra.runtime.auto_wiring.handler_wiring import wire_from_manifest
from omnibase_infra.runtime.event_bus_subcontract_wiring import (
    EventBusSubcontractWiring,
)
from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine
from omnibase_infra.runtime.runtime_local_ingress import (
    ModelRuntimeLocalIngressRoute,
    discover_runtime_local_ingress_routes,
)

from omnimarket.nodes.node_dev_signed_in_read_effect.handlers import (
    handler_dev_signed_in_read,
)
from omnimarket.nodes.node_dev_signed_in_read_effect.handlers.handler_dev_signed_in_read import (
    HandlerDevSignedInRead,
)
from omnimarket.nodes.node_dev_signed_in_read_effect.models.model_dev_signed_in_read import (
    EnumDevSignedInReadOutcome,
    ModelDevSignedInReadCommand,
    ModelDevSignedInReadResult,
)

from ._support import CONTRACT_PATH, PASSWORD_LOCATION, overlay_document, seeded_store
from .fakes import FakeKeycloakAndApi

pytestmark = pytest.mark.unit

NODE_NAME = "node_dev_signed_in_read_effect"
OVERLAY_ROOTS_ENV = "ONEX_SKILL_OVERLAY_ROOTS"
SECRET_STORE_BLOCK = {
    "provider": "infisical",
    "address": "https://secrets.example.invalid",
    "project_id": "11111111-1111-4111-8111-111111111111",
    "environment": "dev",
    "client_id_ref": "dev.client_id",
    "client_secret_ref": "dev.client_secret",
}


def _contract() -> dict[str, Any]:
    document: dict[str, Any] = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    return document


def _topics() -> dict[str, str]:
    topics: dict[str, str] = _contract()["topics"]
    return topics


def _write_overlay(
    root: Path, mutate: Callable[[dict[str, Any]], None] | None = None
) -> Path:
    document = overlay_document()
    document["secret_store"] = None
    if mutate is not None:
        mutate(document)
    node_dir = root / NODE_NAME
    node_dir.mkdir(parents=True)
    (node_dir / "overlay.yaml").write_text(yaml.safe_dump(document), encoding="utf-8")
    return root


class _Wired:
    """The runtime's wiring of one contract file onto an in-memory bus, plus a count of handler runs."""

    def __init__(
        self, bus: EventBusInmemory, handled: list[ModelDevSignedInReadResult]
    ):
        self.bus = bus
        self.handled = handled

    async def send(self, command: ModelDevSignedInReadCommand) -> None:
        envelope = ModelEventEnvelope[object](
            payload=command.model_dump(mode="json"), correlation_id=uuid4()
        )
        await self.bus.publish(
            _topics()["command"], None, envelope.model_dump_json().encode(), None
        )
        for _ in range(100):
            if await self._terminal_count():
                return
            await asyncio.sleep(0.02)

    async def _terminal_count(self) -> int:
        count = 0
        for key in ("completed", "failed"):
            count += len(await self.bus.get_event_history(topic=_topics()[key]))
        return count

    async def payloads(self, key: str) -> list[dict[str, Any]]:
        history = await self.bus.get_event_history(topic=_topics()[key])
        out: list[dict[str, Any]] = [json.loads(m.value)["payload"] for m in history]
        return out


async def _wire(
    monkeypatch: pytest.MonkeyPatch, contract_path: Path = CONTRACT_PATH
) -> _Wired:
    handled: list[ModelDevSignedInReadResult] = []
    real_handle = HandlerDevSignedInRead.handle

    async def recording_handle(
        self: HandlerDevSignedInRead, command: ModelDevSignedInReadCommand
    ) -> ModelDevSignedInReadResult:
        result = await real_handle(self, command)
        handled.append(result)
        return result

    monkeypatch.setattr(HandlerDevSignedInRead, "handle", recording_handle)
    manifest = discover_contracts_from_paths([contract_path])
    assert manifest.total_errors == 0, manifest.errors
    bus = EventBusInmemory(environment="test", group="dev-signed-in-read")
    await bus.start()
    engine = MessageDispatchEngine()
    report = await wire_from_manifest(
        manifest, engine, event_bus=bus, environment="test"
    )
    assert report.total_wired == 1
    assert report.total_failed == 0
    engine.freeze()
    return _Wired(bus, handled)


def _mutated_contract(tmp_path: Path, mutate: Callable[[dict[str, Any]], None]) -> Path:
    document = _contract()
    mutate(document)
    path = tmp_path / "contract.yaml"
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return path


def test_dev_signed_in_read_runtime_route_contract_declares_the_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = _contract()
    topics = contract["topics"]
    assert contract["event_bus"]["subscribe_topics"] == [topics["command"]]
    assert contract["event_bus"]["publish_topics"] == [
        topics["completed"],
        topics["failed"],
    ]
    assert contract["runtime_dispatch"]["command_topic"] == topics["command"]
    assert contract["runtime_dispatch"]["terminal_events"] == {
        "success": topics["completed"],
        "failure": topics["failed"],
    }
    routing = contract["handler_routing"]
    assert routing["routing_strategy"] == "operation_match"
    assert len(routing["handlers"]) == 1
    route = routing["handlers"][0]
    assert route["operation"] == "dev-signed-in-read.run"
    assert route["message_category"] == "command"
    assert route["handler"]["name"] == "HandlerDevSignedInRead"
    # The runtime indexes the dispatcher under the event type; an inbound command's message type is the topic's alias.
    assert route["event_type"] == "omnimarket.dev-signed-in-read-requested"


def test_dev_signed_in_read_runtime_route_broker_grants_cover_the_topics_and_the_consumer_group() -> (
    None
):
    contract = _contract()
    topics = contract["topics"]
    (principal,) = contract["broker_acl_grants"]
    assert principal["principal"] == "${ONEX_ACL_RUNTIME_PRINCIPAL}"
    granted = {
        (g["resource_type"], g["pattern"], g["name"], g["operation"])
        for g in principal["grants"]
    }
    group = "local.omnimarket.node_dev_signed_in_read_effect.consume."
    assert granted == {
        ("TOPIC", "LITERAL", topics["command"], "READ"),
        ("TOPIC", "LITERAL", topics["command"], "DESCRIBE"),
        ("TOPIC", "LITERAL", topics["completed"], "WRITE"),
        ("TOPIC", "LITERAL", topics["completed"], "DESCRIBE"),
        ("TOPIC", "LITERAL", topics["failed"], "WRITE"),
        ("TOPIC", "LITERAL", topics["failed"], "DESCRIBE"),
        ("GROUP", "PREFIXED", group, "READ"),
        ("GROUP", "PREFIXED", group, "DESCRIBE"),
    }


def test_dev_signed_in_read_runtime_route_declared_modules_import() -> None:
    import importlib

    for route in _contract()["handler_routing"]["handlers"]:
        for ref in (route["handler"], route["event_model"]):
            module = importlib.import_module(ref["module"])
            assert hasattr(module, ref["name"]), ref
    handler = _contract()["handler"]
    assert hasattr(importlib.import_module(handler["module"]), handler["class"])


def test_dev_signed_in_read_runtime_route_async_tests_run_under_auto_mode(
    request: pytest.FixtureRequest,
) -> None:
    assert request.config.getini("asyncio_mode") == "auto"


def test_dev_signed_in_read_runtime_route_no_arg_construction_resolves_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_resolution(*args: object, **kwargs: object) -> NoReturn:
        pytest.fail("construction must not resolve environment or network dependencies")

    for name in (
        "load_overlay",
        "UrllibIdentityTransport",
        "SecretStoreSettingsReader",
    ):
        monkeypatch.setattr(handler_dev_signed_in_read, name, unexpected_resolution)
    HandlerDevSignedInRead()


async def test_dev_signed_in_read_runtime_route_completes_a_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The command reaches the handler through the runtime and the answer reaches the completed topic."""
    transport = FakeKeycloakAndApi()
    store = seeded_store()
    monkeypatch.setattr(
        handler_dev_signed_in_read, "UrllibIdentityTransport", lambda: transport
    )
    monkeypatch.setattr(
        handler_dev_signed_in_read, "SecretStoreSettingsReader", lambda **_: store
    )
    monkeypatch.setenv(OVERLAY_ROOTS_ENV, str(_write_overlay(tmp_path / "roots")))
    wired = await _wire(monkeypatch)

    await wired.send(ModelDevSignedInReadCommand(lane="dev", write=True))

    assert [r.outcome for r in wired.handled] == [
        EnumDevSignedInReadOutcome.READ_ACCEPTED
    ]
    (completed,) = await wired.payloads("completed")
    assert completed["outcome"] == "read_accepted"
    assert completed["ok"] is True
    assert await wired.payloads("failed") == []
    assert store.calls == [PASSWORD_LOCATION]
    assert [r.method for r in transport.sent] == ["POST", "GET"]


async def test_dev_signed_in_read_runtime_route_refusal_reaches_the_failed_topic(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    roots = _write_overlay(
        tmp_path / "roots", lambda d: d["keycloak"].update(realm="outside-dev")
    )
    monkeypatch.setenv(OVERLAY_ROOTS_ENV, str(roots))
    wired = await _wire(monkeypatch)

    await wired.send(ModelDevSignedInReadCommand(lane="dev", write=True))

    assert [r.outcome for r in wired.handled] == [
        EnumDevSignedInReadOutcome.REFUSED_NOT_DEV
    ]
    (failed,) = await wired.payloads("failed")
    assert failed["outcome"] == "refused_not_dev"
    assert failed["realm"] == "outside-dev"
    assert await wired.payloads("completed") == []


async def test_dev_signed_in_read_runtime_route_missing_overlay_emits_the_typed_failed_event(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(OVERLAY_ROOTS_ENV, str(tmp_path / "missing"))
    wired = await _wire(monkeypatch)

    await wired.send(ModelDevSignedInReadCommand(lane="dev", write=True))

    assert [r.outcome for r in wired.handled] == [
        EnumDevSignedInReadOutcome.OVERLAY_UNAVAILABLE
    ]
    (failed,) = await wired.payloads("failed")
    # The handler's own typed result, not the runtime's boundary failure envelope (which carries ``failure_class``).
    assert failed["outcome"] == "overlay_unavailable"
    assert failed["ok"] is False
    assert "failure_class" not in failed
    assert await wired.payloads("completed") == []


async def test_dev_signed_in_read_runtime_route_a_typo_in_the_event_type_runs_no_handler(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The control: the declared event type at ``51bdb423ac65`` matched no command, and the handler never ran."""
    monkeypatch.setenv(OVERLAY_ROOTS_ENV, str(_write_overlay(tmp_path / "roots")))

    def typo(document: dict[str, Any]) -> None:
        document["handler_routing"]["handlers"][0]["event_type"] = (
            "omnimarket.dev-signed-in-read"
        )

    wired = await _wire(monkeypatch, _mutated_contract(tmp_path, typo))
    await wired.send(ModelDevSignedInReadCommand(lane="dev", write=False))

    assert wired.handled == []
    (failed,) = await wired.payloads("failed")
    assert failed["failure_class"] == "HandlerDispatchFailureError"
    assert await wired.payloads("completed") == []


async def test_dev_signed_in_read_runtime_route_a_typo_in_the_handler_module_fails_wiring(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def typo(document: dict[str, Any]) -> None:
        document["handler_routing"]["handlers"][0]["handler"]["module"] += "_typo"

    manifest = discover_contracts_from_paths([_mutated_contract(tmp_path, typo)])
    bus = EventBusInmemory(environment="test", group="dev-signed-in-read")
    await bus.start()
    report = await wire_from_manifest(
        manifest, MessageDispatchEngine(), event_bus=bus, environment="test"
    )
    assert report.total_failed == 1
    assert report.total_wired == 0


# The ingress route: POST /skill is answered from ``discover_runtime_local_ingress_routes`` (the dev runtime sets its
# route table with that call, then answers ``unknown_command`` when the requested command name is not a key of it).


def _ingress_lookup(
    routes: dict[str, ModelRuntimeLocalIngressRoute], command_name: str
) -> ModelRuntimeLocalIngressRoute | None:
    """The runtime's own resolution of a requested command name: a dict get, a miss is ``unknown_command``."""
    return routes.get(command_name)


def _routes_of_a_package_holding(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mutate: Callable[[dict[str, Any]], None],
) -> dict[str, ModelRuntimeLocalIngressRoute]:
    """Route discovery over a one-node package whose contract is the node's contract after ``mutate``."""
    package = f"ingress_probe_{abs(hash(str(tmp_path)))}"
    node_dir = tmp_path / package / "nodes" / NODE_NAME
    node_dir.mkdir(parents=True)
    (tmp_path / package / "__init__.py").write_text("")
    document = _contract()
    mutate(document)
    (node_dir / "contract.yaml").write_text(
        yaml.safe_dump(document, sort_keys=False), encoding="utf-8"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    return discover_runtime_local_ingress_routes([package])


def test_dev_signed_in_read_runtime_route_ingress_resolves_the_node_by_name() -> None:
    """The real package: the command name the lanes post is a key of the route table, so it is not ``unknown_command``."""
    routes = discover_runtime_local_ingress_routes(["omnimarket"])
    route = _ingress_lookup(routes, NODE_NAME)
    assert route is not None  # None is what answers ``unknown_command`` at POST /skill
    contract = _contract()
    topics = contract["topics"]
    assert route.command_topic == topics["command"]
    assert route.terminal_events == (topics["completed"], topics["failed"])
    declared = contract["handler_routing"]["handlers"][0]["event_type"]
    assert route.event_type == declared
    # The event type the runtime matches an inbound command on is the alias of its topic.
    assert route.event_type == EventBusSubcontractWiring._derive_event_type_from_topic(
        route.command_topic
    )
    assert route.input_model_name == "ModelDevSignedInReadCommand"


def test_dev_signed_in_read_runtime_route_ingress_answers_unknown_command_for_the_merge_base_contract(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The control: the contract as it stood at the merge base subscribes nothing and no route is discovered."""

    def merge_base_shape(document: dict[str, Any]) -> None:
        document["event_bus"]["subscribe_topics"] = []

    routes = _routes_of_a_package_holding(monkeypatch, tmp_path, merge_base_shape)
    assert _ingress_lookup(routes, NODE_NAME) is None


def test_dev_signed_in_read_runtime_route_ingress_event_type_matches_the_topic_alias(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A changed declared event type gives a route whose event type is not the alias the runtime matches commands on."""
    alias = EventBusSubcontractWiring._derive_event_type_from_topic(
        _topics()["command"]
    )

    def typo(document: dict[str, Any]) -> None:
        document["handler_routing"]["handlers"][0]["event_type"] = (
            "omnimarket.dev-signed-in-read"
        )

    routes = _routes_of_a_package_holding(monkeypatch, tmp_path, lambda _: None)
    route = _ingress_lookup(routes, NODE_NAME)
    assert route is not None
    assert route.event_type == alias
    typo_dir = tmp_path / "typo"
    typo_dir.mkdir()
    typo_routes = _routes_of_a_package_holding(monkeypatch, typo_dir, typo)
    typo_route = _ingress_lookup(typo_routes, NODE_NAME)
    assert typo_route is not None
    assert typo_route.event_type != alias


def test_dev_signed_in_read_runtime_route_one_entry_so_operation_selects_nothing() -> (
    None
):
    """With one ``event_model`` entry the runtime indexes the handler by event type; ``operation`` selects no other.

    The command carries no operation name, so the declared operation is a label the schema requires: it is pinned to
    its literal here and the entry count is pinned to one, so a second entry (where selection would start to matter)
    fails this test and has to say how the payload picks between entries.
    """
    handlers = _contract()["handler_routing"]["handlers"]
    assert [h["operation"] for h in handlers] == ["dev-signed-in-read.run"]
    assert set(ModelDevSignedInReadCommand.model_fields) == {"lane", "write"}
