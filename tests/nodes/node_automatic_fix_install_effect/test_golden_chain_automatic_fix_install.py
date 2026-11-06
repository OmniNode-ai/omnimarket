# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A proven branch must reach each install without waiting for its merge."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

from omnimarket.nodes.node_automatic_fix_install_effect import (
    HandlerAutomaticFixInstall,
    ModelFixInstallOverlay,
    ModelFixInstallRequest,
    ModelFixInstallTarget,
)

pytestmark = pytest.mark.unit
NOW = datetime(2026, 11, 4, 13, 36, 52, tzinfo=UTC)


class World:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.repo = root / "repo"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "dev")
        self.base = self.change(None, "base.txt", "base\n")
        self.git("update-ref", "refs/remotes/origin/dev", self.base)
        self.head = self.change(self.base, "fixed.txt", "branch fix\n")
        self.live = {"first": self.base, "second": self.base}
        self.installs: list[tuple[str, str]] = []
        self.reads: list[str] = []
        self.watcher = root / "watcher.json"
        self.proofs = root / "proofs.json"
        self.write_sources()

    def git(self, *argv: str) -> str:
        done = subprocess.run(
            ["git", "-C", str(self.repo), *argv],
            text=True,
            capture_output=True,
            check=True,
            timeout=30,
            env={
                **scrub_git_location_env(os.environ),
                "GIT_AUTHOR_NAME": "fixture",
                "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
                "GIT_COMMITTER_NAME": "fixture",
                "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
            },
        )
        return done.stdout.strip()

    def change(self, parent: str | None, name: str, text: str) -> str:
        if parent:
            self.git("checkout", "-q", "--detach", parent)
        (self.repo / name).write_text(text)
        self.git("add", name)
        self.git("-c", "commit.gpgsign=false", "commit", "-qm", "fix fixture")
        return self.git("rev-parse", "HEAD")

    def write_sources(self, *, at: datetime = NOW, proven: bool = True) -> None:
        self.watcher.write_text(
            json.dumps(
                {
                    "last_tick": at.isoformat(),
                    "prs": {
                        "fixture#1": {
                            "facts": {
                                "state": "OPEN",
                                "repo": "fixture",
                                "head_sha": self.head,
                                "title": "fix: recorded branch install case",
                                "draft": False,
                                "labels": [],
                            }
                        }
                    },
                }
            )
        )
        self.proofs.write_text(
            json.dumps(
                {
                    "passes": {f"fixture#1@{self.head}": {"at": NOW.isoformat()}}
                    if proven
                    else {},
                    "fails": {},
                }
            )
        )

    def target(self, key: str) -> ModelFixInstallTarget:
        return ModelFixInstallTarget(
            key=key,
            host=f"lab-{key}",
            surface="plugin" if key == "first" else "dispatch",
            repository="fixture",
            clone=self.repo,
            released_ref="origin/dev",
            installed_command=("read-installed", key),
            install_command=("existing-installer", key, "{revision}"),
            verify_command=("observe", key, "{revision}"),
        )

    def overlay(self, *keys: str) -> ModelFixInstallOverlay:
        return ModelFixInstallOverlay(
            state_dir=self.root / "state",
            watcher_state=self.watcher,
            proof_state=self.proofs,
            targets=tuple(self.target(k) for k in (keys or ("first", "second"))),
        )

    def run(
        self, argv: tuple[str, ...], timeout: int
    ) -> subprocess.CompletedProcess[str]:
        if argv[0] == "read-installed":
            self.reads.append(argv[1])
            return subprocess.CompletedProcess(argv, 0, self.live[argv[1]], "")
        if argv[0] == "existing-installer":
            self.installs.append((argv[1], argv[2]))
            self.live[argv[1]] = argv[2]
            return subprocess.CompletedProcess(argv, 0, "installed", "")
        if argv[0] == "observe":
            return subprocess.CompletedProcess(argv, 0, "next call ran", "")
        raise AssertionError(argv)

    def handler(self, **kwargs):
        return HandlerAutomaticFixInstall(
            overlay=self.overlay(), run=self.run, clock=lambda: NOW, **kwargs
        )


def test_recorded_proven_fix_installs_on_both_hosts_before_merge(tmp_path):
    """At the recorded ruling the fix was on a branch while hosts still ran the base."""
    world = World(tmp_path)
    result = world.handler().handle(ModelFixInstallRequest())
    assert result.status == "completed"
    assert world.installs == [("first", world.head), ("second", world.head)]
    assert [r.status for r in result.targets] == ["installed", "installed"]
    assert all(r.installed == world.head and r.observed for r in result.targets)
    assert world.git("rev-parse", "origin/dev") == world.base
    assert (world.root / "state" / "last-run.json").is_file()


def test_already_installed_heads_do_not_reinstall(tmp_path):
    world = World(tmp_path)
    handler = world.handler()
    handler.handle(ModelFixInstallRequest())
    world.installs.clear()
    result = handler.handle(ModelFixInstallRequest())
    assert world.installs == []
    assert [r.status for r in result.targets] == ["current", "current"]


@pytest.mark.parametrize("proven", [False, True])
def test_stale_watcher_or_unproven_branch_never_installs(tmp_path, proven):
    world = World(tmp_path)
    world.write_sources(at=NOW - timedelta(minutes=9) if proven else NOW, proven=proven)
    result = world.handler().handle(ModelFixInstallRequest())
    assert not world.installs
    assert result.status == ("refused" if proven else "completed")


def test_dry_run_reads_every_host_but_never_installs(tmp_path):
    world = World(tmp_path)
    result = world.handler().handle(ModelFixInstallRequest(apply=False))
    assert not world.installs
    assert set(world.reads) == {"first", "second"}
    assert all(
        r.status == "planned" and r.desired == world.head for r in result.targets
    )


def test_conflict_drops_only_that_fix_and_names_it(tmp_path):
    world = World(tmp_path)
    installed = world.change(world.base, "fixed.txt", "previous installed fix\n")
    world.live = dict.fromkeys(world.live, installed)
    other = world.change(world.base, "other.txt", "independent fix\n")
    snapshot = json.loads(world.watcher.read_text())
    snapshot["prs"]["fixture#2"] = {
        "facts": {
            **snapshot["prs"]["fixture#1"]["facts"],
            "head_sha": other,
            "title": "fix: independent",
        }
    }
    world.watcher.write_text(json.dumps(snapshot))
    proofs = json.loads(world.proofs.read_text())
    proofs["passes"][f"fixture#2@{other}"] = {"at": NOW.isoformat()}
    world.proofs.write_text(json.dumps(proofs))
    result = world.handler().handle(ModelFixInstallRequest())
    assert result.status == "partial"
    assert len(world.installs) == 2
    for target in result.targets:
        assert [(d.fix, d.head) for d in target.dropped] == [("fixture#1", world.head)]
        assert target.dropped[0].paths == ("fixed.txt",)
        assert (
            world.git("show", f"{target.installed}:fixed.txt")
            == "previous installed fix"
        )
        assert world.git("show", f"{target.installed}:other.txt") == "independent fix"
        world.git("merge-base", "--is-ancestor", installed, target.installed)
        world.git("merge-base", "--is-ancestor", other, target.installed)


def test_tick_lock_refuses_second_concurrent_tick(tmp_path):
    import fcntl

    world = World(tmp_path)
    state = world.overlay().state_dir
    state.mkdir()
    with (state / "tick.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = world.handler().handle(ModelFixInstallRequest())
    assert result.status == "locked"
    assert not world.installs


def test_bus_cannot_supply_paths_commands_or_time():
    from pydantic import ValidationError

    for key in ("overlay", "targets", "install_command", "now", "watcher_state"):
        with pytest.raises(ValidationError):
            ModelFixInstallRequest.model_validate({key: "untrusted"})


def test_released_ref_advances_without_unproven_fix(tmp_path):
    world = World(tmp_path)
    world.write_sources(proven=False)
    released = world.change(world.base, "released.txt", "landed\n")
    world.git("update-ref", "refs/remotes/origin/dev", released)
    result = world.handler().handle(ModelFixInstallRequest())
    assert result.status == "completed"
    assert world.installs == [("first", released), ("second", released)]


@pytest.mark.parametrize(
    "change",
    [
        "moved-head",
        "draft",
        "held",
        "closed",
        "non-fix",
        "failed-proof",
        "future-proof",
        "naive-proof",
    ],
)
def test_only_current_unheld_exact_head_fix_proofs_qualify(tmp_path, change):
    world = World(tmp_path)
    watcher = json.loads(world.watcher.read_text())
    facts = watcher["prs"]["fixture#1"]["facts"]
    proofs = json.loads(world.proofs.read_text())
    if change == "moved-head":
        facts["head_sha"] = world.base
    elif change == "draft":
        facts["draft"] = True
    elif change == "held":
        facts["labels"] = ["verification-hold"]
    elif change == "closed":
        facts["state"] = "CLOSED"
    elif change == "non-fix":
        facts["title"] = "feat: unrelated feature"
    elif change == "failed-proof":
        proofs["fails"][f"fixture#1@{world.head}"] = {"at": NOW.isoformat()}
    elif change == "future-proof":
        proofs["passes"][f"fixture#1@{world.head}"]["at"] = (
            NOW + timedelta(minutes=1)
        ).isoformat()
    else:
        proofs["passes"][f"fixture#1@{world.head}"]["at"] = NOW.replace(
            tzinfo=None
        ).isoformat()
    world.watcher.write_text(json.dumps(watcher))
    world.proofs.write_text(json.dumps(proofs))
    result = world.handler().handle(ModelFixInstallRequest())
    assert result.status == "completed"
    assert not world.installs


@pytest.mark.parametrize("source", ["watcher", "proofs"])
def test_missing_or_malformed_source_refuses_without_install(tmp_path, source):
    world = World(tmp_path)
    getattr(world, source).write_text("{invalid")
    result = world.handler().handle(ModelFixInstallRequest())
    assert result.status == "refused"
    assert not world.installs


@pytest.mark.parametrize(
    "failure", ["installer", "wrong-readback", "observation", "timeout"]
)
def test_one_failed_host_does_not_block_other_host_or_report_success(tmp_path, failure):
    world = World(tmp_path)

    def run(argv, timeout):
        if argv[0] == "existing-installer" and argv[1] == "first":
            assert (world.root / "state" / "first.pending.json").is_file()
            if failure == "installer":
                return subprocess.CompletedProcess(argv, 7, "", "refused")
            if failure == "wrong-readback":
                return subprocess.CompletedProcess(argv, 0, "", "")
            if failure == "timeout":
                raise subprocess.TimeoutExpired(argv, timeout)
        if argv[0] == "observe" and argv[1] == "first" and failure == "observation":
            return subprocess.CompletedProcess(argv, 5, "", "failed smoke")
        return world.run(argv, timeout)

    handler = HandlerAutomaticFixInstall(
        overlay=world.overlay(), run=run, clock=lambda: NOW
    )
    result = handler.handle(ModelFixInstallRequest())
    assert result.status == "partial"
    assert result.targets[0].status in {"failed", "refused"}
    assert not result.targets[0].observed
    assert result.targets[1].status == "installed"
    if failure == "observation":
        # A later tick must observe again, even though installation already happened.
        retry = handler.handle(ModelFixInstallRequest())
        assert retry.targets[0].status == "failed"
        assert retry.targets[1].status == "current"


def test_proof_withdrawn_during_planning_refuses_that_target(tmp_path):
    world = World(tmp_path)
    calls = 0

    def run(argv, timeout):
        nonlocal calls
        result = world.run(argv, timeout)
        if argv[0] == "read-installed":
            calls += 1
            if calls == 1:
                world.write_sources(proven=False)
        return result

    result = HandlerAutomaticFixInstall(
        overlay=world.overlay(), run=run, clock=lambda: NOW
    ).handle(ModelFixInstallRequest())
    assert result.targets[0].reason == "sources-changed-before-install"
    assert not world.installs


def test_outside_installer_change_invalidates_plan(tmp_path):
    world = World(tmp_path)
    calls = 0

    def run(argv, timeout):
        nonlocal calls
        if argv[0] == "read-installed" and argv[1] == "first":
            calls += 1
            if calls == 2:
                world.live["first"] = world.head
        return world.run(argv, timeout)

    result = HandlerAutomaticFixInstall(
        overlay=world.overlay(), run=run, clock=lambda: NOW
    ).handle(ModelFixInstallRequest())
    assert result.targets[0].reason == "installed-revision-changed-before-install"
    assert world.installs == [("second", world.head)]


def test_disjoint_installed_branch_and_new_fix_form_deterministic_superset(tmp_path):
    world = World(tmp_path)
    installed = world.change(world.base, "installed.txt", "keep me\n")
    world.live = dict.fromkeys(world.live, installed)
    before_head = world.git("rev-parse", "HEAD")
    before_index = world.git("write-tree")
    result = world.handler().handle(ModelFixInstallRequest())
    assert result.status == "completed"
    first, second = result.targets
    assert first.installed == second.installed
    assert world.git("show", f"{first.installed}:installed.txt") == "keep me"
    assert world.git("show", f"{first.installed}:fixed.txt") == "branch fix"
    assert world.git("rev-parse", "HEAD") == before_head
    assert world.git("write-tree") == before_index
    assert world.git("status", "--porcelain") == ""


def test_all_three_install_surfaces_use_the_same_route(tmp_path):
    world = World(tmp_path)
    targets = [world.target("first"), world.target("second")]
    targets.append(
        targets[1].model_copy(update={"key": "agent", "surface": "deploy-agent"})
    )
    world.live["agent"] = world.base
    targets[2] = targets[2].model_copy(
        update={
            "installed_command": ("read-installed", "agent"),
            "install_command": ("existing-installer", "agent", "{revision}"),
            "verify_command": ("observe", "agent", "{revision}"),
        }
    )
    overlay = world.overlay().model_copy(update={"targets": tuple(targets)})
    result = HandlerAutomaticFixInstall(
        overlay=overlay, run=world.run, clock=lambda: NOW
    ).handle(ModelFixInstallRequest())
    assert [r.surface for r in result.targets] == ["plugin", "dispatch", "deploy-agent"]
    assert len(world.installs) == 3


def test_environment_is_lab_only_and_missing_overlay_refuses(monkeypatch):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ModelFixInstallTarget.model_validate({"environment": "prod"})
    monkeypatch.delenv("ONEX_FIX_INSTALL_OVERLAY", raising=False)
    monkeypatch.delenv("ONEX_SKILL_OVERLAY_ROOTS", raising=False)
    result = HandlerAutomaticFixInstall(clock=lambda: NOW).handle(
        ModelFixInstallRequest()
    )
    assert result.status == "refused"
    assert result.findings == ("NodeOverlayError",)


async def test_installed_discovery_wires_command_to_correlated_terminal(
    tmp_path, monkeypatch
):
    import asyncio
    from uuid import uuid4

    import yaml
    from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
    from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
    from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts
    from omnibase_infra.runtime.auto_wiring.handler_wiring import wire_from_manifest
    from omnibase_infra.runtime.auto_wiring.models import ModelAutoWiringManifest
    from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine

    from omnimarket.nodes.node_automatic_fix_install_effect import node

    world = World(tmp_path)
    world.write_sources(at=datetime.now(UTC))
    # A real read-only command for dry-run dispatch; no injected handler or bus.
    target = world.target("first").model_copy(
        update={
            "installed_command": (
                "git",
                "-C",
                str(world.repo),
                "rev-parse",
                "origin/dev",
            )
        }
    )
    overlay = world.overlay("first").model_copy(update={"targets": (target,)})
    config = tmp_path / "overlay.json"
    config.write_text(overlay.model_dump_json())
    monkeypatch.setenv("ONEX_FIX_INSTALL_OVERLAY", str(config))
    contracts = tuple(
        c
        for c in discover_contracts().contracts
        if c.name == "node_automatic_fix_install_effect"
    )
    assert len(contracts) == 1
    raw = yaml.safe_load(Path(node.__file__).with_name("contract.yaml").read_text())
    completed = asyncio.Queue()
    bus = EventBusInmemory(environment="local", group="fix-install-test")
    await bus.start()

    async def collect(message):
        await completed.put((message.topic, json.loads(message.value)))

    try:
        await bus.subscribe(
            raw["terminal_event"], group_id="fix-install-results", on_message=collect
        )
        engine = MessageDispatchEngine()
        report = await wire_from_manifest(
            ModelAutoWiringManifest(contracts=contracts),
            engine,
            event_bus=bus,
            environment="local",
        )
        assert [r.outcome.value for r in report.results] == ["wired"]
        assert set(report.results[0].topics_subscribed) == set(
            raw["event_bus"]["subscribe_topics"]
        )
        engine.freeze()
        correlation = uuid4()
        envelope = ModelEventEnvelope[object](
            payload={"operation": "tick", "apply": False},
            correlation_id=correlation,
            event_type="omnimarket.automatic-fix-install-requested",
        )
        await bus.publish(
            raw["runtime_dispatch"]["command_topic"],
            None,
            envelope.model_dump_json().encode(),
            None,
        )
        topic, event = await asyncio.wait_for(completed.get(), timeout=5)
        assert topic == "onex.evt.omnimarket.automatic-fix-install-completed.v1"
        assert event["correlation_id"] == str(correlation)
        assert event["payload"]["status"] == "completed"
        assert event["payload"]["targets"][0]["desired"] == world.head
        assert event["payload"]["targets"][0]["status"] == "planned"
    finally:
        await bus.close()


def test_host_module_tick_uses_existing_tree_installer_and_observes_it(tmp_path):
    """Exercise the timer's deployed route, including the existing atomic installer."""
    import sys

    world = World(tmp_path)
    world.git("checkout", "-q", "--detach", world.base)
    prefix = "plugins/onex-overlays"
    manifest = world.repo / prefix / ".claude-plugin/plugin.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"name": "onex-overlays"}))
    source = world.repo / prefix / "overlays/sample/overlay.yaml"
    source.parent.mkdir(parents=True)
    source.write_text("enabled: false\n")
    world.git("add", prefix)
    world.git("-c", "commit.gpgsign=false", "commit", "-qm", "released plugin fixture")
    world.base = world.git("rev-parse", "HEAD")
    world.git("update-ref", "refs/remotes/origin/dev", world.base)
    world.head = world.change(
        world.base, f"{prefix}/overlays/sample/overlay.yaml", "enabled: true\n"
    )
    world.write_sources(at=datetime.now(UTC))
    live = tmp_path / "installed-plugin"
    record = tmp_path / "install-record.json"
    # This deployment adapter only supplies the existing installer's typed input.
    # The installer itself stages, verifies, renames and records the actual tree.
    adapter = tmp_path / "installer_adapter.py"
    adapter.write_text(
        "import json, sys\n"
        "from pathlib import Path\n"
        "from omnimarket.nodes.node_overlay_tree_install_effect.node import NodeOverlayTreeInstallEffect, ModelOverlayTreeInstallRequest\n"
        "operation, clone, dest, record, revision = sys.argv[1:]\n"
        "if operation == 'read':\n"
        "    print(json.loads(Path(record).read_text())['last_good']['commit'])\n"
        "else:\n"
        "    request = ModelOverlayTreeInstallRequest(operation=operation, clone=clone, install_path=dest, record=record, ref=revision)\n"
        "    result = NodeOverlayTreeInstallEffect().handle(request)\n"
        "    print(result.model_dump_json())\n"
        "    sys.exit(0 if result.verdict == 'pass' else 1)\n"
    )
    base_command = (sys.executable, str(adapter))

    def command(operation, revision):
        return (
            *base_command,
            operation,
            str(world.repo),
            str(live),
            str(record),
            revision,
        )

    env = {**scrub_git_location_env(os.environ), "PYTHONPATH": str(Path.cwd() / "src")}
    bootstrap = subprocess.run(
        command("install", world.base),
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert bootstrap.returncode == 0, bootstrap.stdout + bootstrap.stderr
    target = world.target("first").model_copy(
        update={
            "installed_command": command("read", "unused"),
            "install_command": command("install", "{revision}"),
            "verify_command": command("verify", "{revision}"),
        }
    )
    overlay = world.overlay("first").model_copy(update={"targets": (target,)})
    config = tmp_path / "deployment-overlay.json"
    config.write_text(overlay.model_dump_json())
    tick = subprocess.run(
        [
            sys.executable,
            "-m",
            "omnimarket.nodes.node_automatic_fix_install_effect.runtime_automatic_fix_install",
            "--overlay",
            str(config),
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert tick.returncode == 0, tick.stdout + tick.stderr
    receipt = json.loads((overlay.state_dir / "last-run.json").read_text())
    assert receipt["status"] == "completed"
    assert receipt["targets"][0]["installed"] == world.head
    assert receipt["targets"][0]["observed"]
    assert (live / "overlays/sample/overlay.yaml").read_text() == "enabled: true\n"
    assert json.loads(record.read_text())["last_good"]["commit"] == world.head
    assert not (overlay.state_dir / "first.pending.json").exists()
    assert world.git("rev-parse", "origin/dev") == world.base
