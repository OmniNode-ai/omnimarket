# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain of the broker permission overlay render (OMN-23589).

A scoped overlay file is loaded the way the node loads it, the request goes onto the contract's command topic, a
binding renders it and the terminal event carries the sorted rows and the operator's two commands. The same
request run directly gives the same result: only the bus differs between the in-memory and the real-broker profile.
"""

from __future__ import annotations

import asyncio
from importlib import resources
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    NodeOverlayNotFoundError,
)
from omnimarket.nodes.node_broker_acl_overlay_render_compute import handlers
from omnimarket.nodes.node_broker_acl_overlay_render_compute.handlers import (
    handler_broker_acl_overlay_render as module,
)
from omnimarket.nodes.node_broker_acl_overlay_render_compute.handlers.handler_broker_acl_overlay_render import (
    HandlerBrokerAclOverlayRender,
    contract_config,
)
from omnimarket.nodes.node_broker_acl_overlay_render_compute.models import (
    ModelBrokerAclRenderRequest,
    ModelBrokerAclRenderResult,
    ModelBrokerAclScopeOverlay,
)
from omnimarket.nodes.node_broker_acl_overlay_render_compute.overlay import (
    load_scope_overlay,
)

pytestmark = pytest.mark.unit

SCOPE = "broker_acls_example"
_NODE = "node_broker_acl_overlay_render_compute"


def _contract() -> dict[str, Any]:
    text = (
        resources.files(f"omnimarket.nodes.{_NODE}")
        .joinpath("contract.yaml")
        .read_text(encoding="utf-8")
    )
    return yaml.safe_load(text)


def _document() -> dict[str, Any]:
    def grant(name: str, operation: str) -> dict[str, str]:
        return {
            "resource_type": "TOPIC",
            "pattern": "LITERAL",
            "name": name,
            "operation": operation,
        }

    return {
        "scope": SCOPE,
        "ticket": "OMN-23589",
        "why": "an example scope",
        "operator_ssh_target": "broker-alias",
        "operator_command": "apply-tool apply --declaration {declaration} --ssh-target {ssh_target}",
        "declaration_path": "overlays/{scope}/overlay.yaml",
        "lane": "dev",
        "host": "dev-lane-broker",
        "compose_project": "omnibase-infra",
        "broker_container": "omnibase-infra-redpanda",
        "superuser_source_container": "omnibase-infra-redpanda-sasl-enable",
        "principals": [
            {
                "principal": "User:zeta-login",
                "role": "the second login",
                "grants": [
                    grant("onex.evt.omnimarket.example.v1", "WRITE"),
                    grant("onex.evt.omnimarket.example.v1", "READ"),
                ],
            },
            {
                "principal": "User:alpha-login",
                "role": "the first login",
                "grants": [grant("onex.cmd.omnimarket.example.v1", "DESCRIBE")],
            },
        ],
    }


def _install(
    root: Path, document: dict[str, Any], scope: str = SCOPE
) -> dict[str, str]:
    target = root / scope
    target.mkdir(parents=True)
    (target / "overlay.yaml").write_text(yaml.safe_dump(document), encoding="utf-8")
    return {"ONEX_SKILL_OVERLAY_ROOTS": str(root)}


def _request(environ: dict[str, str]) -> ModelBrokerAclRenderRequest:
    return ModelBrokerAclRenderRequest(
        scope=SCOPE, overlay=load_scope_overlay(SCOPE, environ)
    )


async def _over_bus(request: ModelBrokerAclRenderRequest) -> ModelBrokerAclRenderResult:
    command = _contract()["runtime_dispatch"]["command_topic"]
    terminal = _contract()["terminal_event"]
    bus = EventBusInmemory(environment="test", group="broker-acl-render")
    await bus.start()
    done: asyncio.Queue[ModelBrokerAclRenderResult] = asyncio.Queue()

    async def on_command(message: Any) -> None:
        result = HandlerBrokerAclOverlayRender().handle(
            ModelBrokerAclRenderRequest.model_validate_json(message.value)
        )
        await bus.publish(terminal, None, result.model_dump_json().encode())

    async def observe(message: Any) -> None:
        await done.put(ModelBrokerAclRenderResult.model_validate_json(message.value))

    try:
        await bus.subscribe_ungrouped(terminal, observe)
        await bus.subscribe_ungrouped(command, on_command)
        await bus.publish(command, None, request.model_dump_json().encode())
        return await asyncio.wait_for(done.get(), timeout=30)
    finally:
        await bus.close()


async def test_golden_chain_renders_sorted_rows_and_the_two_operator_commands(
    tmp_path: Path,
) -> None:
    request = _request(_install(tmp_path, _document()))
    over_bus = await _over_bus(request)
    assert over_bus == HandlerBrokerAclOverlayRender().handle(request)
    assert over_bus.status == "rendered"
    assert over_bus.declared == 3, "falsifier: every declared grant is a row"
    assert [(r.principal, r.operation.value) for r in over_bus.rows] == [
        ("User:alpha-login", "DESCRIBE"),
        ("User:zeta-login", "READ"),
        ("User:zeta-login", "WRITE"),
    ]
    assert over_bus.operator_apply.startswith("apply-tool apply --declaration ")
    assert over_bus.operator_apply.endswith("--ssh-target broker-alias")
    assert over_bus.operator_dry_run == f"{over_bus.operator_apply} --dry-run"
    assert SCOPE in over_bus.operator_apply


def test_row_order_does_not_depend_on_the_order_of_the_overlay(tmp_path: Path) -> None:
    document = _document()
    shuffled = _document()
    shuffled["principals"] = list(reversed(shuffled["principals"]))
    shuffled["principals"][1]["grants"] = list(
        reversed(shuffled["principals"][1]["grants"])
    )
    first = _request(_install(tmp_path / "a", document))
    second = _request(_install(tmp_path / "b", shuffled))
    handler = HandlerBrokerAclOverlayRender()
    assert handler.handle(first).rows == handler.handle(second).rows


def test_the_contract_declares_the_command_and_terminal_topics() -> None:
    contract = _contract()
    command = contract["runtime_dispatch"]["command_topic"]
    assert command in contract["event_bus"]["subscribe_topics"]
    assert contract["terminal_event"] in contract["event_bus"]["publish_topics"]
    assert contract["terminal_event"] in contract["externally_consumed_topics"]
    assert command == "onex.cmd.omnimarket.broker-acl-overlay-render-requested.v1"
    assert (
        contract["terminal_event"]
        == "onex.evt.omnimarket.broker-acl-overlay-rendered.v1"
    )
    assert contract["node_type"] == "compute"


def test_a_missing_overlay_refuses_instead_of_rendering_nothing(tmp_path: Path) -> None:
    with pytest.raises(NodeOverlayNotFoundError):
        load_scope_overlay(SCOPE, {"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)})


def test_an_unknown_key_in_the_overlay_refuses_the_load(tmp_path: Path) -> None:
    document = _document()
    document["invented"] = "x"
    environ = _install(tmp_path, document)
    with pytest.raises(NodeOverlayError):
        load_scope_overlay(SCOPE, environ)


def test_injected_break_the_refusal_set_is_the_contracts_not_the_handlers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Break: let DELETE through on a topic by editing the contract config; the refusal disappears."""
    document = _document()
    document["principals"][0]["grants"][0]["operation"] = "DELETE"
    request = _request(_install(tmp_path, document))
    handler = HandlerBrokerAclOverlayRender()
    assert handler.handle(request).status == "refused"
    broken = {**contract_config(), "delete_allowed_resource_types": ["GROUP", "TOPIC"]}
    monkeypatch.setattr(module, "contract_config", lambda: broken)
    assert handler.handle(request).status == "rendered"


def test_handlers_package_is_the_one_the_contract_names() -> None:
    declared = _contract()["handler_routing"]["handlers"][0]["handler"]["module"]
    assert declared == f"{handlers.__name__}.handler_broker_acl_overlay_render"
    assert ModelBrokerAclScopeOverlay.model_fields.keys() >= {"principals", "lane"}
