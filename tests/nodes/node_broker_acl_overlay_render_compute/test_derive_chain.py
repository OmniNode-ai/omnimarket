# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Derive broker permission rows from contract topics and declared principals (OMN-19998, issue L1-04).

Golden chain: contracts are scanned the way the node scans them, the request goes onto the contract's command topic, the
derive operation answers on the terminal topic with sorted rows. Error chain: every refusal carries its finding code.
Seam: a topic a contract declares with no row (derived or declared) refuses the check; the broker write node's commands stay unreadable.
Failing control: the operations, group prefix and refused set are the contract's config, not the handler's.
"""

from __future__ import annotations

import asyncio
from importlib import resources
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.models.broker_acl import (
    EnumBrokerAclFindingCode,
    ModelBrokerAclDeriveRequest,
    ModelBrokerAclDeriveResult,
    ModelBrokerAclPrincipalsOverlay,
    ModelBrokerAclRow,
)
from omnimarket.nodes.node_broker_acl_overlay_render_compute.handlers import (
    handler_broker_acl_derive as module,
)
from omnimarket.nodes.node_broker_acl_overlay_render_compute.handlers.handler_broker_acl_derive import (
    HandlerBrokerAclDerive,
)
from omnimarket.nodes.node_broker_acl_overlay_render_compute.handlers.handler_broker_acl_overlay_render import (
    contract_config,
)
from omnimarket.nodes.node_broker_acl_overlay_render_compute.overlay import (
    load_principals_overlay,
)
from omnimarket.nodes.node_broker_acl_reconcile_effect.handlers.contract_topics import (
    load_topic_uses,
)

pytestmark = pytest.mark.unit

_NODE = "node_broker_acl_overlay_render_compute"
_CODES = EnumBrokerAclFindingCode
CMD = "onex.cmd.omnimarket.alpha-requested.v1"
EVT = "onex.evt.omnimarket.alpha-completed.v1"


def _contract() -> dict[str, Any]:
    text = (
        resources.files(f"omnimarket.nodes.{_NODE}")
        .joinpath("contract.yaml")
        .read_text(encoding="utf-8")
    )
    return yaml.safe_load(text)


def _write_contract(
    root: Path, name: str, subscribe: list[str], publish: list[str]
) -> None:
    target = root / name
    target.mkdir(parents=True)
    document = {
        "name": name,
        "event_bus": {"subscribe_topics": subscribe, "publish_topics": publish},
    }
    (target / "contract.yaml").write_text(yaml.safe_dump(document), encoding="utf-8")


def _principals(*pairs: tuple[str, tuple[str, ...]]) -> ModelBrokerAclPrincipalsOverlay:
    return ModelBrokerAclPrincipalsOverlay.model_validate(
        {
            "scope": "broker_acl_principals",
            "ticket": "OMN-19998",
            "why": "a test scope",
            "principals": [
                {"principal": principal, "role": "a login", "nodes": list(nodes)}
                for principal, nodes in pairs
            ],
        }
    )


def _request(
    root: Path,
    principals: ModelBrokerAclPrincipalsOverlay,
    declared: tuple[ModelBrokerAclRow, ...] = (),
) -> ModelBrokerAclDeriveRequest:
    return ModelBrokerAclDeriveRequest(
        scope="broker_acl_principals",
        topic_uses=load_topic_uses(root),
        principals=principals,
        declared_rows=declared,
    )


def _fixture(root: Path) -> None:
    _write_contract(root, "node_alpha_effect", [CMD], [EVT])


async def _over_bus(request: ModelBrokerAclDeriveRequest) -> ModelBrokerAclDeriveResult:
    command = _contract()["runtime_dispatch"]["command_topic"]
    terminal = _contract()["terminal_event"]
    bus = EventBusInmemory(environment="test", group="broker-acl-derive")
    await bus.start()
    done: asyncio.Queue[ModelBrokerAclDeriveResult] = asyncio.Queue()

    async def on_command(message: Any) -> None:
        result = HandlerBrokerAclDerive().handle(
            ModelBrokerAclDeriveRequest.model_validate_json(message.value)
        )
        await bus.publish(terminal, None, result.model_dump_json().encode())

    async def observe(message: Any) -> None:
        await done.put(ModelBrokerAclDeriveResult.model_validate_json(message.value))

    try:
        await bus.subscribe_ungrouped(terminal, observe)
        await bus.subscribe_ungrouped(command, on_command)
        await bus.publish(command, None, request.model_dump_json().encode())
        return await asyncio.wait_for(done.get(), timeout=30)
    finally:
        await bus.close()


async def test_golden_chain_derives_sorted_rows_from_the_contract_topics(
    tmp_path: Path,
) -> None:
    _fixture(tmp_path)
    request = _request(
        tmp_path, _principals(("User:alpha-login", ("node_alpha_effect",)))
    )
    over_bus = await _over_bus(request)
    assert over_bus == HandlerBrokerAclDerive().handle(request)
    assert over_bus.status == "derived"
    assert over_bus.contract_topics == 2
    assert over_bus.uncovered_topics == ()
    got = [
        (r.resource_type.value, r.pattern.value, r.name, r.operation.value)
        for r in over_bus.rows
    ]
    assert got == sorted(got)
    assert ("TOPIC", "LITERAL", CMD, "READ") in got
    assert ("TOPIC", "LITERAL", EVT, "WRITE") in got
    assert (
        "GROUP",
        "PREFIXED",
        "local.omnimarket.node_alpha_effect.consume.v1.",
        "READ",
    ) in got
    assert over_bus.derived_rows == len(over_bus.rows) == 6, (
        "falsifier: 2 publish ops + 2 subscribe ops + 2 group ops"
    )
    assert all(r.principal == "User:alpha-login" for r in over_bus.rows)


def test_a_new_topic_with_no_row_fails_the_render_check(tmp_path: Path) -> None:
    """The seam: a contract gains a topic; no principal covers it and no declared row names it."""
    _fixture(tmp_path)
    _write_contract(
        tmp_path, "node_beta_compute", ["onex.cmd.omnimarket.beta-requested.v1"], []
    )
    request = _request(
        tmp_path, _principals(("User:alpha-login", ("node_alpha_effect",)))
    )
    result = HandlerBrokerAclDerive().handle(request)
    assert result.status == "refused"
    assert result.uncovered_topics == ("onex.cmd.omnimarket.beta-requested.v1",)
    assert [f.code for f in result.findings] == [_CODES.TOPIC_WITHOUT_ROW]
    assert len(result.rows) == 6, (
        "alpha's rows are still derived: only the check is refused"
    )


def test_a_declared_row_covers_a_topic_no_principal_derives(tmp_path: Path) -> None:
    _fixture(tmp_path)
    _write_contract(
        tmp_path, "node_beta_compute", ["onex.cmd.omnimarket.beta-requested.v1"], []
    )
    declared = ModelBrokerAclRow(
        principal="User:other-login",
        resource_type="TOPIC",
        pattern="LITERAL",
        name="onex.cmd.omnimarket.beta-requested.v1",
        operation="READ",
    )
    result = HandlerBrokerAclDerive().handle(
        _request(
            tmp_path,
            _principals(("User:alpha-login", ("node_alpha_effect",))),
            (declared,),
        )
    )
    assert result.status == "derived"
    assert result.uncovered_topics == ()


def test_error_chain_one_finding_code_per_refusal(tmp_path: Path) -> None:
    _fixture(tmp_path)
    handler = HandlerBrokerAclDerive()
    wildcard = handler.handle(
        _request(tmp_path, _principals(("User:*", ("node_alpha_effect",))))
    )
    assert _CODES.PRINCIPAL_INVALID in {f.code for f in wildcard.findings}
    assert wildcard.rows == ()
    bare = handler.handle(
        _request(tmp_path, _principals(("alpha-login", ("node_alpha_effect",))))
    )
    assert _CODES.PRINCIPAL_INVALID in {f.code for f in bare.findings}
    ghost = handler.handle(
        _request(
            tmp_path,
            _principals(
                ("User:alpha-login", ("node_alpha_effect", "node_missing_effect"))
            ),
        )
    )
    assert [f.code for f in ghost.findings] == [_CODES.NODE_WITHOUT_CONTRACT]
    conflict = handler.handle(
        _request(
            tmp_path,
            _principals(
                ("User:a-login", ("node_alpha_effect",)),
                ("User:b-login", ("node_alpha_effect",)),
            ),
        )
    )
    assert _CODES.NODE_PRINCIPAL_CONFLICT in {f.code for f in conflict.findings}


def test_a_lab_login_is_never_derived_a_read_of_the_write_nodes_commands(
    tmp_path: Path,
) -> None:
    command = contract_config()["scm_command_topics"][0]
    _write_contract(tmp_path, "node_reader_effect", [command], [])
    result = HandlerBrokerAclDerive().handle(
        _request(tmp_path, _principals(("User:reader-login", ("node_reader_effect",))))
    )
    assert _CODES.FORBIDDEN_SCM_GRANT in {f.code for f in result.findings}
    assert result.rows == ()


def test_load_topic_uses_is_deterministic_and_names_a_broken_contract(
    tmp_path: Path,
) -> None:
    _write_contract(tmp_path, "node_b_effect", [CMD], [EVT])
    _write_contract(tmp_path, "node_a_effect", [CMD], [])
    first = load_topic_uses(tmp_path)
    assert first == load_topic_uses(tmp_path)
    assert [u.node for u in first] == [
        "node_a_effect",
        "node_b_effect",
        "node_b_effect",
    ]
    (tmp_path / "node_b_effect" / "contract.yaml").write_text(
        "- not a mapping\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="node_b_effect"):
        load_topic_uses(tmp_path)


def test_the_real_contracts_load_and_name_this_nodes_own_topics() -> None:
    uses = load_topic_uses()
    mine = {(u.direction.value, u.topic) for u in uses if u.node == _NODE}
    contract = _contract()
    assert ("subscribe", contract["runtime_dispatch"]["command_topic"]) in mine
    assert ("publish", contract["terminal_event"]) in mine
    assert len({u.node for u in uses}) > 100


def test_the_principals_overlay_loads_through_the_model(tmp_path: Path) -> None:
    target = tmp_path / "broker_acl_principals"
    target.mkdir()
    (target / "overlay.yaml").write_text(
        yaml.safe_dump(
            {
                "scope": "broker_acl_principals",
                "ticket": "OMN-19998",
                "why": "w",
                "principals": [
                    {
                        "principal": "User:x-login",
                        "role": "r",
                        "nodes": ["node_alpha_effect"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    loaded = load_principals_overlay(
        "broker_acl_principals", {"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)}
    )
    assert loaded.principals[0].nodes == ("node_alpha_effect",)


def test_injected_break_operations_and_group_prefix_are_the_contracts_not_the_handlers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Break: change the contract's derive settings; the derived rows follow, so the handler holds none of them."""
    _fixture(tmp_path)
    request = _request(
        tmp_path, _principals(("User:alpha-login", ("node_alpha_effect",)))
    )
    handler = HandlerBrokerAclDerive()
    assert handler.handle(request).derived_rows == 6
    broken = {
        **contract_config(),
        "derive_publish_operations": ["WRITE"],
        "derive_group_prefix_template": "x.{node}.",
    }
    monkeypatch.setattr(module, "contract_config", lambda: broken)
    changed = handler.handle(request)
    assert changed.derived_rows == 5
    assert any(r.name == "x.node_alpha_effect." for r in changed.rows)
