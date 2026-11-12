# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Gate the coverage of every topic use a change adds to a node contract by a declared grant (OMN-24005).

Golden chain: a covered request answers over the bus. Error chain: an added use with no declared grant refuses naming it.
Failing control: a new topic with no declared grant refuses; the same change with the grant is covered. A captured publish needs a
write row. A use the base already declares is not judged (no exception list exists). The entry point is run over a real git
repository: base commit, then a change, then the check. The operations are the contract's config, not the handler's.
Exemplars followed: test_derive_chain.py (bus helper, fixture contracts) and the render node's own tests.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from importlib import resources
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.models.broker_acl import (
    EnumBrokerAclFindingCode,
    ModelBrokerAclCoverageRequest,
    ModelBrokerAclCoverageResult,
    ModelBrokerAclRow,
    ModelContractTopicUse,
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
from omnimarket.nodes.node_broker_acl_reconcile_effect import (
    runtime_broker_acl_coverage_gate as entry_point,
)
from omnimarket.nodes.node_broker_acl_reconcile_effect.handlers.contract_topics_at_ref import (
    load_topic_uses_at_ref,
)

pytestmark = pytest.mark.unit

_NODE = "node_broker_acl_overlay_render_compute"
_CODES = EnumBrokerAclFindingCode
NODES_DIR = "src/omnimarket/nodes"
CMD = "onex.cmd.omnimarket.alpha-requested.v1"
EVT = "onex.evt.omnimarket.alpha-completed.v1"
CAPTURED = "onex.cmd.omniintelligence.content-captured.v1"


def _contract() -> dict[str, Any]:
    text = (
        resources.files(f"omnimarket.nodes.{_NODE}")
        .joinpath("contract.yaml")
        .read_text(encoding="utf-8")
    )
    return yaml.safe_load(text)


def _row(name: str, operation: str) -> ModelBrokerAclRow:
    return ModelBrokerAclRow(
        principal="User:test",
        resource_type="TOPIC",
        pattern="LITERAL",
        name=name,
        operation=operation,
    )


def _use(node: str, topic: str, direction: str) -> ModelContractTopicUse:
    return ModelContractTopicUse(node=node, topic=topic, direction=direction)


def _request(
    uses: tuple[ModelContractTopicUse, ...],
    base: tuple[ModelContractTopicUse, ...] = (),
    declared: tuple[ModelBrokerAclRow, ...] = (),
) -> ModelBrokerAclCoverageRequest:
    return ModelBrokerAclCoverageRequest(
        topic_uses=uses, base_topic_uses=base, declared_rows=declared
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


async def test_golden_chain_covered_over_the_bus() -> None:
    """An added publish whose WRITE and DESCRIBE rows are declared answers covered on the contract's terminal topic."""
    request = _request(
        (_use("node_alpha_effect", EVT, "publish"),),
        declared=(_row(EVT, "WRITE"), _row(EVT, "DESCRIBE")),
    )
    result = await _over_bus(request)
    assert result == HandlerBrokerAclCoverageGate().handle(request)
    assert (result.status, result.new_uses, result.uncovered_new_uses) == (
        "covered",
        1,
        0,
    )


async def test_error_chain_uncovered_use_refuses_over_the_bus() -> None:
    """An added use with no declared row answers refused, naming node, direction and topic, on the same terminal topic."""
    request = _request((_use("node_alpha_effect", CMD, "subscribe"),))
    result = await _over_bus(request)
    assert result.status == "refused"
    assert [(f.code, f.detail) for f in result.findings] == [
        (
            _CODES.TOPIC_OPERATION_WITHOUT_ROW,
            f"node_alpha_effect subscribe: {CMD}",
        )
    ]


def test_new_topic_with_no_declared_grant_refuses() -> None:
    """Failing control: a topic the change adds with no declared row refuses and names it; with its rows declared it is covered."""
    uses = (_use("node_alpha_effect", EVT, "publish"),)
    refused = HandlerBrokerAclCoverageGate().handle(_request(uses))
    assert refused.status == "refused"
    assert EVT in refused.findings[0].detail
    covered = HandlerBrokerAclCoverageGate().handle(
        _request(uses, declared=(_row(EVT, "WRITE"), _row(EVT, "DESCRIBE")))
    )
    assert covered.status == "covered"


def test_content_captured_publish_needs_a_write_row() -> None:
    """The captured-content topic's produce grant was found missing: a publish with only READ and DESCRIBE declared refuses."""
    uses = (_use("node_event_emit_effect", CAPTURED, "publish"),)
    read_only = (_row(CAPTURED, "READ"), _row(CAPTURED, "DESCRIBE"))
    assert (
        HandlerBrokerAclCoverageGate()
        .handle(_request(uses, declared=read_only))
        .findings
    )
    assert (
        HandlerBrokerAclCoverageGate()
        .handle(_request(uses, declared=(*read_only, _row(CAPTURED, "WRITE"))))
        .status
        == "covered"
    )


def test_a_use_the_base_already_declares_is_not_judged() -> None:
    """The debt that predates the check is the base's: the same use present in both is not refused, and no list holds it."""
    use = (_use("node_old_effect", CMD, "subscribe"),)
    result = HandlerBrokerAclCoverageGate().handle(_request(use, base=use))
    assert (result.status, result.new_uses) == ("covered", 0)


def test_the_operations_are_the_contracts_config_not_the_handlers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failing control: with the contract naming WRITE only for a publish, one WRITE row covers it; with the real config it does not."""
    uses = (_use("node_alpha_effect", EVT, "publish"),)
    declared = (_row(EVT, "WRITE"),)
    assert (
        HandlerBrokerAclCoverageGate().handle(_request(uses, declared=declared)).status
        == "refused"
    )
    config = {**contract_config(), "derive_publish_operations": ["WRITE"]}
    monkeypatch.setattr(module, "contract_config", lambda: config)
    assert (
        HandlerBrokerAclCoverageGate().handle(_request(uses, declared=declared)).status
        == "covered"
    )


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@example.invalid",
            *args,
        ],
        check=True,
        capture_output=True,
        env=scrub_git_location_env(os.environ),
    )


def _write_node(
    root: Path, name: str, subscribe: list[str], publish: list[str]
) -> None:
    target = root / NODES_DIR / name
    target.mkdir(parents=True, exist_ok=True)
    document = {
        "name": name,
        "event_bus": {"subscribe_topics": subscribe, "publish_topics": publish},
    }
    (target / "contract.yaml").write_text(yaml.safe_dump(document), encoding="utf-8")


def _repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q", "-b", "base")
    _write_node(tmp_path, "node_old_effect", [CMD], [])
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base")
    return tmp_path


def test_the_base_side_reads_the_contracts_of_a_revision(tmp_path: Path) -> None:
    """The loader returns what the base commit declares even after the working tree changed; an unknown ref raises."""
    root = _repo(tmp_path)
    _write_node(root, "node_old_effect", [CMD], [EVT])
    uses = load_topic_uses_at_ref(root, "base", NODES_DIR)
    assert [(u.node, u.topic, u.direction.value) for u in uses] == [
        ("node_old_effect", CMD, "subscribe")
    ]
    with pytest.raises(ValueError, match="git ls-tree"):
        load_topic_uses_at_ref(root, "no-such-ref", NODES_DIR)


def test_entry_point_refuses_a_new_topic_and_names_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Over a real repository: a contract that gains a topic after the base commit exits 1 naming node, direction and topic."""
    root = _repo(tmp_path)
    _write_node(root, "node_new_effect", [], [EVT])
    code = entry_point.main(["--base-ref", "base", "--repo-root", str(root)])
    out = capsys.readouterr().out
    assert code == 1
    assert f"node_new_effect publish: {EVT}" in out


def test_entry_point_passes_when_no_use_is_added(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Positive control: the same repository with no change since the base exits 0 and reports nothing added."""
    root = _repo(tmp_path)
    assert entry_point.main(["--base-ref", "base", "--repo-root", str(root)]) == 0
    assert "0 topic uses added" in capsys.readouterr().out


def test_entry_point_with_an_unreadable_base_exits_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A base ref git cannot read is exit 2 with the cause on stderr, never a pass."""
    root = _repo(tmp_path)
    assert entry_point.main(["--base-ref", "nope", "--repo-root", str(root)]) == 2
    assert "git ls-tree" in capsys.readouterr().err
