# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Gate the coverage of every contract topic use by a declared row or a frozen baseline (OMN-24005).

Golden chain: a covered request answers over the bus. Error chain: an uncovered use refuses naming it. Failing control:
a new topic with no declared grant refuses; the same request with the grant is covered. A captured publish needs a write
row. The baseline holds a node at its frozen count. The operations are the contract's config, not the handler's.
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
    EnumAclResourceType,
    EnumBrokerAclFindingCode,
    ModelBrokerAclCoverageRequest,
    ModelBrokerAclCoverageResult,
    ModelBrokerAclRow,
    ModelContractTopicUse,
    ModelCoverageBaselineEntry,
)
from omnimarket.nodes.node_broker_acl_overlay_render_compute.handlers import (
    handler_broker_acl_coverage_gate as module,
)
from omnimarket.nodes.node_broker_acl_overlay_render_compute.handlers.handler_broker_acl_coverage_gate import (
    HandlerBrokerAclCoverageGate,
)
from omnimarket.nodes.node_broker_acl_overlay_render_compute.handlers.handler_broker_acl_overlay_render import (
    contract_config,
)
from omnimarket.nodes.node_broker_acl_reconcile_effect.handlers.contract_topics import (
    load_topic_uses,
)
from omnimarket.nodes.node_broker_acl_reconcile_effect.handlers.coverage_baseline import (
    load_coverage_baseline,
)
from omnimarket.nodes.node_broker_acl_reconcile_effect.handlers.handler_broker_acl_reconcile import (
    aggregate_declared_grants,
)

pytestmark = pytest.mark.unit

_NODE = "node_broker_acl_overlay_render_compute"
_CODES = EnumBrokerAclFindingCode


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


def _row(
    resource_type: str, pattern: str, name: str, operation: str
) -> ModelBrokerAclRow:
    return ModelBrokerAclRow(
        principal="User:test",
        resource_type=resource_type,
        pattern=pattern,
        name=name,
        operation=operation,
    )


def _request(
    uses: tuple[ModelContractTopicUse, ...],
    declared: tuple[ModelBrokerAclRow, ...] = (),
    baseline: tuple[ModelCoverageBaselineEntry, ...] = (),
) -> ModelBrokerAclCoverageRequest:
    return ModelBrokerAclCoverageRequest(
        topic_uses=uses, declared_rows=declared, baseline=baseline
    )


async def _over_bus(
    request: ModelBrokerAclCoverageRequest,
) -> ModelBrokerAclCoverageResult:
    command = _contract()["runtime_dispatch"]["command_topic"]
    terminal = _contract()["terminal_event"]
    bus = EventBusInmemory(environment="test", group="broker-acl-coverage")
    await bus.start()
    done: asyncio.Queue[ModelBrokerAclCoverageResult] = asyncio.Queue()

    async def on_command(message: Any) -> None:
        result = HandlerBrokerAclCoverageGate().handle(
            ModelBrokerAclCoverageRequest.model_validate_json(message.value)
        )
        await bus.publish(terminal, None, result.model_dump_json().encode())

    async def observe(message: Any) -> None:
        await done.put(ModelBrokerAclCoverageResult.model_validate_json(message.value))

    try:
        await bus.subscribe_ungrouped(terminal, observe)
        await bus.subscribe_ungrouped(command, on_command)
        await bus.publish(command, None, request.model_dump_json().encode())
        return await asyncio.wait_for(done.get(), timeout=30)
    finally:
        await bus.close()


def test_golden_chain_a_covered_request_answers_covered_over_the_bus(
    tmp_path: Path,
) -> None:
    """A covered request answers status covered over the contract's command and terminal topics."""
    _write_contract(
        tmp_path, "node_alpha_effect", ["onex.cmd.alpha.v1"], ["onex.evt.alpha.v1"]
    )
    uses = load_topic_uses(tmp_path)
    declared = (
        _row("TOPIC", "LITERAL", "onex.cmd.alpha.v1", "READ"),
        _row("TOPIC", "LITERAL", "onex.cmd.alpha.v1", "DESCRIBE"),
        _row("TOPIC", "LITERAL", "onex.evt.alpha.v1", "WRITE"),
        _row("TOPIC", "LITERAL", "onex.evt.alpha.v1", "DESCRIBE"),
    )
    request = _request(uses, declared)
    over_bus = asyncio.run(_over_bus(request))
    assert over_bus == HandlerBrokerAclCoverageGate().handle(request)
    assert over_bus.status == "covered"
    assert over_bus.uncovered_uses == 0


def test_error_chain_an_uncovered_use_refuses_naming_it(tmp_path: Path) -> None:
    """A request with an uncovered use answers refused with TOPIC_OPERATION_WITHOUT_ROW naming node, direction and topic."""
    _write_contract(
        tmp_path, "node_alpha_effect", ["onex.cmd.alpha.v1"], ["onex.evt.alpha.v1"]
    )
    uses = load_topic_uses(tmp_path)
    declared = (
        _row("TOPIC", "LITERAL", "onex.cmd.alpha.v1", "READ"),
        _row("TOPIC", "LITERAL", "onex.cmd.alpha.v1", "DESCRIBE"),
        _row("TOPIC", "LITERAL", "onex.evt.alpha.v1", "DESCRIBE"),
    )
    request = _request(uses, declared)
    over_bus = asyncio.run(_over_bus(request))
    assert over_bus.status == "refused"
    assert _CODES.TOPIC_OPERATION_WITHOUT_ROW in {f.code for f in over_bus.findings}
    assert any(
        f.detail == "node_alpha_effect publish: onex.evt.alpha.v1"
        for f in over_bus.findings
    )


def test_new_topic_with_no_declared_grant_refuses(tmp_path: Path) -> None:
    """A node that publishes a topic no declared row names refuses; the same request with the row is covered."""
    _write_contract(tmp_path, "node_alpha_effect", [], ["onex.evt.alpha.v1"])
    uses = load_topic_uses(tmp_path)
    refused = HandlerBrokerAclCoverageGate().handle(_request(uses))
    assert refused.status == "refused"
    assert _CODES.TOPIC_OPERATION_WITHOUT_ROW in {f.code for f in refused.findings}
    assert any(
        f.detail == "node_alpha_effect publish: onex.evt.alpha.v1"
        for f in refused.findings
    )
    declared = (
        _row("TOPIC", "LITERAL", "onex.evt.alpha.v1", "WRITE"),
        _row("TOPIC", "LITERAL", "onex.evt.alpha.v1", "DESCRIBE"),
    )
    covered = HandlerBrokerAclCoverageGate().handle(_request(uses, declared))
    assert covered.status == "covered"


def test_content_captured_publish_needs_a_write_row(tmp_path: Path) -> None:
    """A captured publish topic with only READ and DESCRIBE rows refuses; with WRITE it is covered."""
    _write_contract(
        tmp_path,
        "node_event_emit_effect",
        [],
        ["onex.cmd.omniintelligence.content-captured.v1"],
    )
    uses = load_topic_uses(tmp_path)
    read_only = (
        _row(
            "TOPIC", "LITERAL", "onex.cmd.omniintelligence.content-captured.v1", "READ"
        ),
        _row(
            "TOPIC",
            "LITERAL",
            "onex.cmd.omniintelligence.content-captured.v1",
            "DESCRIBE",
        ),
    )
    refused = HandlerBrokerAclCoverageGate().handle(_request(uses, read_only))
    assert refused.status == "refused"
    with_write = (
        *read_only,
        _row(
            "TOPIC", "LITERAL", "onex.cmd.omniintelligence.content-captured.v1", "WRITE"
        ),
    )
    covered = HandlerBrokerAclCoverageGate().handle(_request(uses, with_write))
    assert covered.status == "covered"


def test_baseline_holds_a_node_at_its_frozen_count(tmp_path: Path) -> None:
    """A node with two uncovered uses and a baseline of 2 is covered; baseline 1 refuses; baseline 3 is stale."""
    _write_contract(
        tmp_path, "node_alpha_effect", [], ["onex.evt.a.v1", "onex.evt.b.v1"]
    )
    uses = load_topic_uses(tmp_path)
    held = HandlerBrokerAclCoverageGate().handle(
        _request(
            uses,
            (),
            (ModelCoverageBaselineEntry(node="node_alpha_effect", uncovered_uses=2),),
        )
    )
    assert held.status == "covered"
    assert held.baselined_uses == 2
    low = HandlerBrokerAclCoverageGate().handle(
        _request(
            uses,
            (),
            (ModelCoverageBaselineEntry(node="node_alpha_effect", uncovered_uses=1),),
        )
    )
    assert low.status == "refused"
    assert _CODES.TOPIC_OPERATION_WITHOUT_ROW in {f.code for f in low.findings}
    high = HandlerBrokerAclCoverageGate().handle(
        _request(
            uses,
            (),
            (ModelCoverageBaselineEntry(node="node_alpha_effect", uncovered_uses=3),),
        )
    )
    assert high.status == "refused"
    assert _CODES.BASELINE_STALE in {f.code for f in high.findings}


def test_baseline_entry_for_a_node_with_no_uses_is_stale(tmp_path: Path) -> None:
    """A baseline entry for a node with no topic uses is BASELINE_STALE."""
    _write_contract(tmp_path, "node_alpha_effect", [], ["onex.evt.a.v1"])
    uses = load_topic_uses(tmp_path)
    result = HandlerBrokerAclCoverageGate().handle(
        _request(
            uses,
            (),
            (ModelCoverageBaselineEntry(node="node_ghost_effect", uncovered_uses=1),),
        )
    )
    assert result.status == "refused"
    assert _CODES.BASELINE_STALE in {f.code for f in result.findings}


def test_the_operations_are_the_contracts_not_the_handlers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A row set with only WRITE covers a publish use when the config says so; with the real config it does not."""
    _write_contract(tmp_path, "node_alpha_effect", [], ["onex.evt.alpha.v1"])
    uses = load_topic_uses(tmp_path)
    write_row = (_row("TOPIC", "LITERAL", "onex.evt.alpha.v1", "WRITE"),)
    monkeypatch.setattr(
        module,
        "contract_config",
        lambda: {**contract_config(), "derive_publish_operations": ["WRITE"]},
    )
    assert (
        HandlerBrokerAclCoverageGate().handle(_request(uses, write_row)).status
        == "covered"
    )
    monkeypatch.undo()
    assert (
        HandlerBrokerAclCoverageGate().handle(_request(uses, write_row)).status
        == "refused"
    )


def test_packaged_contracts_are_covered_or_baselined() -> None:
    """The real gate: the packaged contracts' topic uses are covered or held by the frozen baseline."""
    repo_root = Path(__file__).resolve().parents[3]
    reconcile_contract = yaml.safe_load(
        resources.files("omnimarket.nodes.node_broker_acl_reconcile_effect")
        .joinpath("contract.yaml")
        .read_text(encoding="utf-8")
    )
    baseline_path = (
        repo_root / reconcile_contract["broker_acl_reconcile"]["coverage_baseline_path"]
    )
    baseline = load_coverage_baseline(baseline_path)
    uses = load_topic_uses()
    declared = tuple(
        ModelBrokerAclRow(**row)
        for row in aggregate_declared_grants()
        if row["resource_type"] == EnumAclResourceType.TOPIC.value
    )
    result = HandlerBrokerAclCoverageGate().handle(_request(uses, declared, baseline))
    assert result.status == "covered", f"findings: {result.findings}"
