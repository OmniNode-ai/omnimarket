# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Replay the reduced recorded tree: missing worker policy and two stale root files.

The paths and omission are recorded; data values and script bodies are reduced.
No controller, worker, service manager or live installation is started here.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from omnimarket.nodes.node_host_tree_install_effect.handlers import handler_tree_install
from omnimarket.nodes.node_host_tree_install_effect.models.model_tree_install import (
    ModelTreeInstallOverlay,
    ModelTreeInstallRequest,
)
from omnimarket.nodes.node_host_tree_install_effect.node import (
    NodeHostTreeInstallEffect,
)


def write_tree(root: Path, files: dict[str, str]) -> None:
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def test_entry_point_passes_core_contract_compliance() -> None:
    """Replay the guard's recorded custom handle() violation on this node."""
    from omnibase_core.models.nodes.handler_contract_compliance.model_compliance_node_source import (
        ModelComplianceNodeSource,
    )
    from omnibase_core.models.nodes.handler_contract_compliance.model_handler_contract_compliance_input import (
        ModelHandlerContractComplianceInput,
    )
    from omnibase_core.models.nodes.no_utcnow_check.model_source_file import (
        ModelSourceFile,
    )
    from omnibase_core.nodes.node_imperative_contract_guard_check_effect.handler_arch_handler_contract_compliance import (
        HandlerArchHandlerContractCompliance,
    )

    node_dir = NodeHostTreeInstallEffect.contract_path.parent
    relative = "src/omnimarket/nodes/node_host_tree_install_effect"
    report = HandlerArchHandlerContractCompliance().handle(
        ModelHandlerContractComplianceInput(
            repo="omnimarket",
            nodes=[
                ModelComplianceNodeSource(
                    node_dir=relative,
                    contract_yaml=NodeHostTreeInstallEffect.contract_path.read_text(),
                    node_py=(node_dir / "node.py").read_text(),
                    handlers=[
                        ModelSourceFile(
                            path=f"{relative}/handlers/handler_tree_install.py",
                            source=(
                                node_dir / "handlers/handler_tree_install.py"
                            ).read_text(),
                        )
                    ],
                )
            ],
        )
    )
    assert report.total == 1
    assert report.new_violations == []


def test_recorded_missing_policy_and_stale_root_files(tmp_path: Path) -> None:
    recorded = json.loads(
        (Path(__file__).parent / "fixtures/recorded_drain_tree.json").read_text()
    )
    source_root = tmp_path / "source"
    install_root = tmp_path / "installed"
    source = source_root / "merge-drain"
    installed = install_root / "skills/merge-drain"
    write_tree(source, recorded["source"])
    write_tree(installed, recorded["installed"])
    policy = installed / "landing_worker_policy.json"
    with pytest.raises(FileNotFoundError):
        policy.read_bytes()
    for name in ("goal_contract_map.json", "landing_floors.json"):
        assert (installed / name).read_bytes() != (source / name).read_bytes()

    result = NodeHostTreeInstallEffect().handle(
        ModelTreeInstallRequest(
            source_root=source_root,
            install_root=install_root,
        )
    )
    assert result.status == "installed", result.detail
    assert json.loads(policy.read_bytes())["ledger_error_chars"] == 1400
    for name, content in recorded["source"].items():
        assert (installed / name).read_text() == content
    assert result.file_count == len(recorded["source"])


@pytest.fixture
def install_request(tmp_path: Path) -> ModelTreeInstallRequest:
    source_root = tmp_path / "source"
    source_root.mkdir()
    write_tree(source_root / "merge-drain", {"root.json": '{"current": true}\n'})
    return ModelTreeInstallRequest(
        source_root=source_root, install_root=tmp_path / "installed"
    )


def test_whole_tree_includes_future_files_links_modes_and_empty_directories(
    install_request: ModelTreeInstallRequest,
) -> None:
    source = install_request.source_root / "merge-drain"
    write_tree(
        source,
        {
            ".hidden": "hidden",
            "future/deep/policy.new": "new",
            "scripts/run": "exit 0\n",
        },
    )
    (source / "scripts/run").chmod(0o751)
    (source / "empty").mkdir()
    (source / "policy-link").symlink_to("future/deep/policy.new")
    (source / "external-link").symlink_to("../../outside")
    node = NodeHostTreeInstallEffect()
    result = node.handle(install_request)
    assert result.status == "installed", result.detail
    installed = install_request.install_root / "skills/merge-drain"
    assert handler_tree_install.tree_snapshot(
        installed
    ) == handler_tree_install.tree_snapshot(source)
    assert stat.S_IMODE((installed / "scripts/run").stat().st_mode) == 0o751
    assert (installed / "empty").is_dir()
    assert os.readlink(installed / "external-link") == "../../outside"
    assert (
        node.handle(install_request.model_copy(update={"mode": "verify"})).status
        == "verified"
    )


def test_destination_only_generated_receipt_and_unrelated_skill_survive(
    install_request: ModelTreeInstallRequest,
) -> None:
    destination = install_request.install_root / "skills/merge-drain"
    write_tree(
        destination,
        {"scripts/landing-controller.sha256": "generated\n", "root.json": "old"},
    )
    write_tree(install_request.install_root / "skills/other", {"keep": "other"})
    node = NodeHostTreeInstallEffect()
    result = node.handle(install_request)
    assert result.status == "installed", result.detail
    assert (
        destination / "scripts/landing-controller.sha256"
    ).read_text() == "generated\n"
    assert (destination / "root.json").read_text() == '{"current": true}\n'
    assert (install_request.install_root / "skills/other/keep").read_text() == "other"
    assert (Path(result.previous_tree) / "root.json").read_text() == "old"
    second = node.handle(install_request)
    assert second.status == "unchanged"
    assert second.tree_sha256 == result.tree_sha256
    assert second.previous_tree == ""


def test_dry_run_writes_nothing_and_verify_detects_drift(
    install_request: ModelTreeInstallRequest,
) -> None:
    node = NodeHostTreeInstallEffect()
    assert (
        node.handle(install_request.model_copy(update={"mode": "dry-run"})).status
        == "dry-run"
    )
    assert not install_request.install_root.exists()
    assert (
        node.handle(install_request.model_copy(update={"mode": "verify"})).status
        == "refused"
    )
    assert node.handle(install_request).status == "installed"
    assert (
        node.handle(install_request.model_copy(update={"mode": "verify"})).status
        == "verified"
    )
    (install_request.install_root / "skills/merge-drain/root.json").write_text("drift")
    assert (
        node.handle(install_request.model_copy(update={"mode": "verify"})).status
        == "refused"
    )


@pytest.mark.parametrize(
    "relative", ["../escape", "/escape", "", "a/../b", "a//b", "a\\b"]
)
def test_bad_layout_refuses_before_any_write(
    install_request: ModelTreeInstallRequest,
    relative: str,
) -> None:
    overlay = ModelTreeInstallOverlay(
        source_relative="merge-drain", destination_relative=relative
    )
    result = NodeHostTreeInstallEffect().handle(
        install_request.model_copy(update={"overlay": overlay})
    )
    assert result.status == "refused"
    assert not install_request.install_root.exists()


def test_overlapping_or_symlinked_layout_refuses(
    install_request: ModelTreeInstallRequest,
) -> None:
    node = NodeHostTreeInstallEffect()
    overlap = install_request.model_copy(
        update={
            "install_root": install_request.source_root,
            "overlay": ModelTreeInstallOverlay(
                source_relative="merge-drain", destination_relative="merge-drain/inside"
            ),
        }
    )
    assert node.handle(overlap).status == "refused"
    install_request.install_root.mkdir()
    (install_request.install_root / "skills").symlink_to(
        install_request.source_root, target_is_directory=True
    )
    assert node.handle(install_request).status == "refused"
    assert not (install_request.source_root / ".merge-drain.install.lock").exists()


def test_source_type_changes_replace_conflicts_without_following_old_links(
    install_request: ModelTreeInstallRequest,
    tmp_path: Path,
) -> None:
    source = install_request.source_root / "merge-drain"
    write_tree(
        source,
        {"new_directory/child": "new", "new_file": "file", "linked/child": "new"},
    )
    outside = tmp_path / "outside"
    write_tree(outside, {"child": "untouched"})
    destination = install_request.install_root / "skills/merge-drain"
    write_tree(destination, {"new_directory": "old file", "new_file/old_child": "old"})
    (destination / "linked").symlink_to(outside, target_is_directory=True)
    result = NodeHostTreeInstallEffect().handle(install_request)
    assert result.status == "installed", result.detail
    assert (destination / "new_directory/child").read_text() == "new"
    assert (destination / "new_file").read_text() == "file"
    assert not (destination / "linked").is_symlink()
    assert (outside / "child").read_text() == "untouched"


def test_copy_failure_leaves_old_tree_intact(
    install_request: ModelTreeInstallRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = install_request.install_root / "skills/merge-drain"
    write_tree(destination, {"root.json": "old"})

    def fail_copy(*args: object, **kwargs: object) -> None:
        raise OSError("recorded copy failure")

    monkeypatch.setattr(handler_tree_install.shutil, "copytree", fail_copy)
    result = NodeHostTreeInstallEffect().handle(install_request)
    assert result.status == "refused"
    assert "recorded copy failure" in result.detail
    assert (destination / "root.json").read_text() == "old"
    assert not list(destination.parent.glob("*.previous"))


def test_failed_post_install_readback_restores_previous_tree(
    install_request: ModelTreeInstallRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = install_request.install_root / "skills/merge-drain"
    write_tree(destination, {"root.json": "old"})
    rename = Path.rename

    def corrupt_after_swap(path: Path, target: Path) -> Path:
        result = rename(path, target)
        if path.name == "tree":
            (target / "root.json").write_text("corrupt")
        return result

    monkeypatch.setattr(Path, "rename", corrupt_after_swap)
    result = NodeHostTreeInstallEffect().handle(install_request)
    assert result.status == "refused"
    assert "installed tree differs" in result.detail
    assert (destination / "root.json").read_text() == "old"


def test_empty_source_refuses_without_replacing_install(
    install_request: ModelTreeInstallRequest,
) -> None:
    (install_request.source_root / "merge-drain/root.json").unlink()
    result = NodeHostTreeInstallEffect().handle(install_request)
    assert result.status == "refused"
    assert "no regular files" in result.detail
    assert not install_request.install_root.exists()


def test_failed_restore_names_recoverable_backup(
    install_request: ModelTreeInstallRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = install_request.install_root / "skills/merge-drain"
    write_tree(destination, {"root.json": "old"})
    rename = Path.rename

    def fail_swap_and_restore(path: Path, target: Path) -> Path:
        if path.name == "tree" or path.name.endswith(".previous"):
            raise OSError("recorded rename failure")
        return rename(path, target)

    monkeypatch.setattr(Path, "rename", fail_swap_and_restore)
    result = NodeHostTreeInstallEffect().handle(install_request)
    assert result.status == "failed"
    assert "restore failed" in result.detail
    assert (Path(result.previous_tree) / "root.json").read_text() == "old"


def test_lock_contention_is_reported_without_copying(
    install_request: ModelTreeInstallRequest,
) -> None:
    import fcntl

    parent = install_request.install_root / "skills"
    parent.mkdir(parents=True)
    with (parent / ".merge-drain.install.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert NodeHostTreeInstallEffect().handle(install_request).status == "locked"
        assert not (parent / "merge-drain").exists()
    assert NodeHostTreeInstallEffect().handle(install_request).status == "installed"


async def test_declared_runtime_command_installs_recorded_tree(tmp_path: Path) -> None:
    """Discover and dispatch the install through its contract's actual bus adapter."""
    import asyncio
    from uuid import uuid4

    import yaml
    from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
    from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
    from omnibase_infra.event_bus.models.model_event_message import ModelEventMessage
    from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts
    from omnibase_infra.runtime.auto_wiring.handler_wiring import wire_from_manifest
    from omnibase_infra.runtime.auto_wiring.models import ModelAutoWiringManifest
    from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine

    source_root, install_root = tmp_path / "source", tmp_path / "install"
    recorded = json.loads(
        (Path(__file__).parent / "fixtures/recorded_drain_tree.json").read_text()
    )
    write_tree(source_root / "merge-drain", recorded["source"])
    write_tree(install_root / "skills/merge-drain", recorded["installed"])
    contract = yaml.safe_load(NodeHostTreeInstallEffect.contract_path.read_text())
    contracts = tuple(
        c for c in discover_contracts().contracts if c.name == contract["name"]
    )
    assert len(contracts) == 1
    command, completed = (
        contract["runtime_dispatch"]["command_topic"],
        contract["terminal_event"],
    )
    results: asyncio.Queue[dict[str, object]] = asyncio.Queue()
    bus = EventBusInmemory(environment="local", group="host-tree-install")
    await bus.start()

    async def collect(message: ModelEventMessage) -> None:
        assert message.topic == "onex.evt.omnimarket.host-tree-install-completed.v1"
        await results.put(json.loads(message.value))

    try:
        await bus.subscribe(completed, group_id="install-results", on_message=collect)
        engine = MessageDispatchEngine()
        report = await wire_from_manifest(
            ModelAutoWiringManifest(contracts=contracts),
            engine,
            event_bus=bus,
            environment="local",
        )
        assert report.results[0].outcome.value == "wired"
        assert report.results[0].topics_subscribed == (command,)
        engine.freeze()
        correlation = uuid4()
        envelope = ModelEventEnvelope[object](
            payload={
                "source_root": str(source_root),
                "install_root": str(install_root),
            },
            correlation_id=correlation,
            event_type="omnimarket.host-tree-install-requested",
        )
        await bus.publish(command, None, envelope.model_dump_json().encode(), None)
        event = await asyncio.wait_for(results.get(), timeout=3)
        assert event["correlation_id"] == str(correlation)
        payload = event["payload"]
        assert isinstance(payload, dict)
        assert payload["status"] == "installed", payload
        assert payload["file_count"] == len(recorded["source"])
        assert (
            install_root / "skills/merge-drain/landing_worker_policy.json"
        ).read_text() == recorded["source"]["landing_worker_policy.json"]
    finally:
        await bus.close()
