# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared checks for the two example-envelope route nodes.

Each node's test directory calls these with its own route; the assertions are
the behaviour the public API pinned for the route (anonymous, stateless, the
example envelope), ported as behaviour against the mounted ASGI app.
"""

from __future__ import annotations

import ast
import datetime as dt
import importlib
import itertools
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

SRC = Path(__file__).resolve().parents[2] / "src" / "omnimarket"
SHARED_PACKAGES = (
    SRC / "handlers" / "envelope_example",
    SRC / "models" / "model_onex_envelope.py",
    SRC / "models" / "model_envelope_example.py",
    SRC / "protocols" / "protocol_envelope_example_source.py",
)
OLD_SHARED_CONTRACT_ID = "node_envelope_processor:1.0.0"
TOP_LEVEL_KEYS = {
    "envelope_id",
    "envelope_version",
    "correlation_id",
    "causation_id",
    "source_node",
    "source_node_id",
    "target_node",
    "target_node_id",
    "handler_type",
    "operation",
    "timestamp",
    "is_response",
    "success",
    "error",
    "error_details",
    "metadata",
    "payload",
    "tenant_id",
    "tenant_slug",
    "node_id",
    "workflow_id",
    "job_id",
}


@dataclass(frozen=True)
class Route:
    node: str
    method: str
    path: str

    @property
    def package(self) -> str:
        return f"omnimarket.nodes.{self.node}"

    @property
    def contract_path(self) -> Path:
        return SRC / "nodes" / self.node / "contract.yaml"

    def contract(self) -> dict[str, Any]:
        loaded = yaml.safe_load(self.contract_path.read_text(encoding="utf-8"))
        assert isinstance(loaded, dict)
        return loaded

    def factory(self) -> Any:
        http_app = importlib.import_module(f"{self.package}.handlers.http_app")
        return http_app.create_app

    @property
    def other_method(self) -> str:
        return "GET" if self.method == "POST" else "POST"


POST_ROUTE = Route("node_envelope_example_post_effect", "POST", "/envelopes/example")
GET_ROUTE = Route("node_envelope_example_get_effect", "GET", "/v1/envelopes/example")
ROUTES = (POST_ROUTE, GET_ROUTE)


class FakeIdentitySource:
    """The in-memory profile's identity source: counting ids and a fixed clock."""

    def __init__(self) -> None:
        self._counter = itertools.count(1)
        self.instant = dt.datetime(2026, 11, 3, 12, 0, tzinfo=dt.UTC)

    def new_id(self) -> uuid.UUID:
        return uuid.UUID(int=next(self._counter))

    def now(self) -> dt.datetime:
        return self.instant


class DictDocuments:
    """A ProtocolOverlayDocument holding named documents in memory."""

    def __init__(self, documents: dict[str, dict[str, Any]]) -> None:
        self._documents = documents

    def read(self, name: str) -> dict[str, Any] | None:
        return self._documents.get(name)


def call(client: TestClient, route: Route, **kwargs: Any) -> Any:
    return client.request(route.method, route.path, **kwargs)


def build_client(route: Route, **kwargs: Any) -> TestClient:
    app = route.factory()(**kwargs)
    assert isinstance(app, FastAPI)
    return TestClient(app)


# --- the route binding the gateway table pinned ------------------------------


def check_contract_binds_exactly_this_route(route: Route) -> None:
    contract = route.contract()
    ingress = contract["http_ingress"]
    assert ingress["methods"] == [route.method]
    assert ingress["path"] == route.path
    assert contract["http_mount"]["path_prefix"] == route.path
    assert contract["http_mount"]["factory"] == (
        f"{route.package}.handlers.http_app:create_app"
    )
    assert ingress["input_schema"] == {
        "type": "object",
        "properties": {},
        "additionalProperties": True,
    }
    # The pre-port contract id lives in the description, one contract per route.
    assert OLD_SHARED_CONTRACT_ID in contract["description"]
    assert contract["name"] == route.node


def check_two_contracts_cover_the_two_routes_without_overlap() -> None:
    bound = {
        (
            r.contract()["http_ingress"]["methods"][0],
            r.contract()["http_ingress"]["path"],
        )
        for r in ROUTES
    }
    assert bound == {("POST", "/envelopes/example"), ("GET", "/v1/envelopes/example")}
    prefixes = [r.contract()["http_mount"]["path_prefix"] for r in ROUTES]
    assert len(set(prefixes)) == len(prefixes)
    for left in prefixes:
        for right in prefixes:
            if left != right:
                assert not right.startswith(left + "/")
    for prefix in prefixes:
        assert prefix not in {"/health", "/ready", "/health/detailed", "/skill"}


def check_handler_binding_names_the_importable_handler(route: Route) -> None:
    binding = route.contract()["http_ingress"]["handler"]
    module = importlib.import_module(binding["module"])
    handler = getattr(module, binding["name"])
    assert callable(handler.handle)
    handler_routing = route.contract()["handler_routing"]["handlers"][0]["handler"]
    assert handler_routing == binding


# --- anonymous, stateless example envelope ----------------------------------


def check_serves_the_example_envelope(route: Route) -> None:
    source = FakeIdentitySource()
    client = build_client(route, source=source)
    response = call(client, route)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    body = response.json()
    assert set(body) == TOP_LEVEL_KEYS
    settings = route.contract()["config"]["envelope_example"]
    assert body["operation"] == settings["operation"]
    assert body["source_node"] == settings["source_node"]
    assert body["envelope_version"] == settings["envelope_version"]
    assert body["metadata"]["tags"] == settings["tags"]
    assert body["metadata"]["headers"] == {}
    assert body["is_response"] is False
    assert body["success"] is None
    assert body["payload"]["tenant_slug"] == settings["payload"]["tenant_slug"]
    assert body["payload"]["tenant_name"] == settings["payload"]["tenant_name"]
    assert body["payload"]["plan"] == settings["payload"]["plan"]
    assert uuid.UUID(body["payload"]["tenant_id"])
    assert body["timestamp"].startswith("2026-11-03T12:00:00")
    for nullable in (
        "causation_id",
        "source_node_id",
        "target_node",
        "target_node_id",
        "handler_type",
        "error",
        "error_details",
        "tenant_id",
        "tenant_slug",
        "node_id",
        "workflow_id",
        "job_id",
    ):
        assert body[nullable] is None


def check_each_call_is_a_fresh_example(route: Route) -> None:
    client = build_client(route)
    first = call(client, route).json()
    second = call(client, route).json()
    for key in ("envelope_id", "correlation_id"):
        assert first[key] != second[key]
    assert first["payload"]["tenant_id"] != second["payload"]["tenant_id"]
    assert first["payload"]["tenant_slug"] == second["payload"]["tenant_slug"]


def check_anonymous_and_stateless(route: Route) -> None:
    client = build_client(route, source=FakeIdentitySource())
    assert call(client, route).status_code == 200
    # A credential, valid or not, is never required and never refused.
    for headers in (
        {"x-api-key": "not-a-real-key"},
        {"authorization": "Bearer not-a-token"},
    ):
        assert call(client, route, headers=headers).status_code == 200
    # No body is consumed: any body is accepted and changes nothing.
    arbitrary = call(client, route, content=b"{not json", headers={"x-test": "1"})
    assert arbitrary.status_code == 200
    assert call(client, route, json={"anything": [1, 2, 3]}).status_code == 200


def check_other_methods_and_paths_are_not_served(route: Route) -> None:
    client = build_client(route)
    assert client.request(route.other_method, route.path).status_code == 405
    assert client.get(route.path + "/extra").status_code == 404
    assert client.get("/").status_code == 404


def check_docs_are_not_published(route: Route) -> None:
    client = build_client(route)
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


# --- AC2: nothing literal or environmental outside contract and overlay -------


OVERLAY_VALUES: dict[str, Any] = {
    "source_node": "overlay-node-name",
    "envelope_version": "9.8.7",
    "operation": "OVERLAY_OPERATION",
    "tags": {"overlay-tag": "overlay-value"},
    "payload": {
        "tenant_slug": "overlay-slug",
        "tenant_name": "Overlay Tenant",
        "plan": "overlay-plan",
    },
}


def check_overlay_values_reach_the_response(route: Route) -> None:
    documents = DictDocuments({route.node: OVERLAY_VALUES})
    client = build_client(route, overlay=documents, source=FakeIdentitySource())
    body = call(client, route).json()
    assert body["source_node"] == "overlay-node-name"
    assert body["envelope_version"] == "9.8.7"
    assert body["operation"] == "OVERLAY_OPERATION"
    assert body["metadata"]["tags"] == {"overlay-tag": "overlay-value"}
    assert body["payload"]["tenant_slug"] == "overlay-slug"
    assert body["payload"]["tenant_name"] == "Overlay Tenant"
    assert body["payload"]["plan"] == "overlay-plan"
    assert uuid.UUID(body["payload"]["tenant_id"])


def check_environment_changes_nothing(route: Route, monkeypatch: Any) -> None:
    baseline = call(build_client(route, source=FakeIdentitySource()), route).json()
    for name in (
        "ONEX_NODE_NAME",
        "ONEX_API_VERSION",
        "APP_NAME",
        "APP_VERSION",
        "ONEX_ENV",
    ):
        monkeypatch.setenv(name, "environment-value")
    after = call(build_client(route, source=FakeIdentitySource()), route).json()
    assert after == baseline
    assert "environment-value" not in str(after)


def check_partial_overlay_overrides_only_what_it_names(route: Route) -> None:
    documents = DictDocuments({route.node: {"operation": "ONLY_THIS"}})
    body = call(build_client(route, overlay=documents), route).json()
    settings = route.contract()["config"]["envelope_example"]
    assert body["operation"] == "ONLY_THIS"
    assert body["source_node"] == settings["source_node"]
    assert body["payload"]["plan"] == settings["payload"]["plan"]


def check_broken_overlay_refuses_to_build(route: Route) -> None:
    factory = route.factory()
    for broken in (
        {"unknown_field": "x"},
        {"operation": ""},
        {"tags": "not-a-mapping"},
        {"payload": {"tenant_slug": 5}},
        {"envelope_version": None},
    ):
        try:
            factory(overlay=DictDocuments({route.node: broken}))
        except ValueError:
            continue
        raise AssertionError(f"a broken overlay {broken!r} built an app")


def check_contract_settings_missing_refuses_to_build(
    route: Route, monkeypatch: Any, tmp_path: Path
) -> None:
    from omnimarket.handlers.envelope_example import settings as loader

    contract = route.contract()
    for key in ("source_node", "envelope_version", "operation"):
        mutilated = yaml.safe_load(yaml.safe_dump(contract))
        del mutilated["config"]["envelope_example"][key]
        path = tmp_path / f"{key}.yaml"
        path.write_text(yaml.safe_dump(mutilated), encoding="utf-8")
        try:
            loader.load_settings(path, overlay=None)
        except ValueError:
            continue
        raise AssertionError(f"settings without {key} were accepted")
    no_config = yaml.safe_load(yaml.safe_dump(contract))
    del no_config["config"]
    path = tmp_path / "no_config.yaml"
    path.write_text(yaml.safe_dump(no_config), encoding="utf-8")
    try:
        loader.load_settings(path, overlay=None)
    except ValueError:
        return
    raise AssertionError("a contract without config was accepted")


def check_mismatched_handler_binding_refuses_to_build(
    route: Route, monkeypatch: Any, tmp_path: Path
) -> None:
    from omnimarket.handlers.envelope_example import asgi

    binding = route.contract()["http_ingress"]["handler"]
    handler_class = getattr(importlib.import_module(binding["module"]), binding["name"])
    contract = yaml.safe_load(yaml.safe_dump(route.contract()))
    contract["http_ingress"]["handler"]["name"] = "SomeOtherHandler"
    path = tmp_path / "contract.yaml"
    path.write_text(yaml.safe_dump(contract), encoding="utf-8")
    try:
        asgi.build_example_app(path, handler_class=handler_class)
    except ValueError:
        return
    raise AssertionError("a mismatched handler binding was accepted")


def _string_constants(tree: ast.AST) -> list[str]:
    values: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            values.append(node.value)
    return values


def _node_python_files(route: Route) -> list[Path]:
    files = sorted((SRC / "nodes" / route.node).rglob("*.py"))
    for shared in SHARED_PACKAGES:
        files.extend(sorted(shared.rglob("*.py")) if shared.is_dir() else [shared])
    return files


def check_no_environment_read_and_no_literal_values(route: Route) -> None:
    forbidden_names = {"environ", "getenv", "putenv"}
    forbidden_imports = {"os"}
    settings = route.contract()["config"]["envelope_example"]
    literals = {
        settings["source_node"],
        settings["envelope_version"],
        settings["operation"],
        *settings["payload"].values(),
        *settings["tags"].keys(),
        *settings["tags"].values(),
    }
    files = _node_python_files(route)
    assert files
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert (
                    not {a.name.split(".")[0] for a in node.names} & forbidden_imports
                ), path
            if isinstance(node, ast.ImportFrom):
                assert (node.module or "").split(".")[0] not in forbidden_imports, path
            if isinstance(node, ast.Attribute):
                assert node.attr not in forbidden_names, path
            if isinstance(node, ast.Name):
                assert node.id not in forbidden_names, path
        strings = set(_string_constants(tree))
        assert not strings & {str(v) for v in literals}, (path, strings & literals)


def check_installed_overlay_reaches_the_no_argument_factory(route: Route) -> None:
    """The runtime calls the factory with no arguments; the lane overlay is bound."""
    from omnimarket.handlers.container.container_overlay_dependencies import (
        bind_overlay,
    )
    from tests.overlay_bound_support import in_memory_overlay

    bind_overlay(
        in_memory_overlay(documents={route.node: yaml.safe_dump(OVERLAY_VALUES)})
    )
    try:
        body = call(TestClient(route.factory()()), route).json()
    finally:
        bind_overlay(None)
    assert body["source_node"] == "overlay-node-name"
    assert body["envelope_version"] == "9.8.7"
    assert body["payload"]["plan"] == "overlay-plan"
    unbound = call(TestClient(route.factory()()), route).json()
    assert (
        unbound["source_node"]
        == route.contract()["config"]["envelope_example"]["source_node"]
    )


async def check_golden_chain(route: Route) -> None:
    """Contract -> command topic -> runtime dispatch -> handler -> built envelope."""
    from omnibase_core.enums.enum_node_kind import EnumNodeKind
    from omnibase_core.models.dispatch.model_dispatch_route import ModelDispatchRoute
    from omnibase_core.models.dispatch.model_handler_ref import ModelHandlerRef
    from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
    from omnibase_infra.enums import EnumDispatchStatus, EnumMessageCategory
    from omnibase_infra.runtime.auto_wiring import discover_contracts_from_paths
    from omnibase_infra.runtime.auto_wiring.handler_wiring import (
        _make_dispatch_callback,
    )
    from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine

    from omnimarket.models.model_envelope_example import ModelEnvelopeExampleRequest
    from omnimarket.models.model_onex_envelope import ModelOnexEnvelope

    manifest = discover_contracts_from_paths([route.contract_path])
    assert manifest.total_errors == 0
    contract = route.contract()
    entry = contract["handler_routing"]["handlers"][0]
    binding = entry["handler"]
    handler_class = getattr(importlib.import_module(binding["module"]), binding["name"])
    command = contract["runtime_dispatch"]["command_topic"]
    assert command in contract["event_bus"]["subscribe_topics"]
    assert (
        contract["runtime_dispatch"]["terminal_events"]["success"]
        in (contract["event_bus"]["publish_topics"])
    )
    from omnimarket.handlers.envelope_example.builder import EnvelopeExampleBuilder
    from omnimarket.handlers.envelope_example.settings import load_settings

    handler = handler_class(
        EnvelopeExampleBuilder(
            load_settings(route.contract_path, None), FakeIdentitySource()
        )
    )
    engine = MessageDispatchEngine()
    engine.register_dispatcher(
        dispatcher_id="envelope-example-proof",
        dispatcher=_make_dispatch_callback(
            handler,
            ModelHandlerRef(**entry["event_model"]),
            handler_node_kind=EnumNodeKind.EFFECT,
        ),
        category=EnumMessageCategory.COMMAND,
        message_types={entry["event_model"]["name"], entry["event_type"]},
    )
    engine.register_route(
        ModelDispatchRoute(
            route_id="envelope-example-proof-route",
            topic_pattern=command,
            message_category=EnumMessageCategory.COMMAND,
            handler_id="envelope-example-proof",
        )
    )
    engine.freeze()
    result = await engine.dispatch(
        topic=command,
        envelope=ModelEventEnvelope(payload=ModelEnvelopeExampleRequest()),
    )
    assert result.status == EnumDispatchStatus.SUCCESS, result.error_message
    (built,) = result.output_events
    assert isinstance(built, ModelOnexEnvelope)
    settings = contract["config"]["envelope_example"]
    assert built.operation == settings["operation"]
    assert built.source_node == settings["source_node"]
    assert built.payload["tenant_slug"] == settings["payload"]["tenant_slug"]
