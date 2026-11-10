# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Retirement falsifiers through the scheduled host reconcile handler."""

import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_infra.event_bus.models.model_event_message import ModelEventMessage
from pydantic import BaseModel

from omnimarket.models.model_host_reconcile import ModelHostReconcileRunResult
from omnimarket.nodes.node_host_reconcile_effect.handlers import (
    handler_host_reconcile_effect as effect,
)
from omnimarket.nodes.node_host_reconcile_effect.handlers.adapter_publisher import (
    publish_topics,
)
from omnimarket.nodes.node_host_reconcile_effect.handlers.handler_host_reconcile_effect import (
    _ReconcilePass,
)
from omnimarket.nodes.node_host_reconcile_effect.handlers.retired_repositories import (
    load_retirement_policy,
    retired_repository_findings,
)
from tests.test_host_reconcile_effect import (
    RecordingEvaluator,
    RecordingPublisher,
    command,
    handler,
    run_git,
    setup_workspace,
)

pytestmark = pytest.mark.unit

RETIRED_BY_RULING = (
    "onex_change_control",
    "ycbench_onex",
    "omnibase_compat",
)


def retired_clone(root: Path, name: str = "omnibase_compat") -> Path:
    clone = root / name
    clone.mkdir()
    run_git(clone, "init", "--initial-branch=dev")
    run_git(clone, "config", "user.name", "Fixture")
    run_git(clone, "config", "user.email", "fixture")
    (clone / "tracked").write_text("retired history\n")
    run_git(clone, "add", "tracked")
    run_git(clone, "commit", "-m", "fixture")
    return clone


def test_retired_clone_is_deleted_after_a_restorable_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = setup_workspace(tmp_path, monkeypatch)
    clone = retired_clone(root)
    head = run_git(clone, "rev-parse", "HEAD")
    publisher = RecordingPublisher()
    result = handler(RecordingEvaluator(), publisher).handle(command(root))
    assert result.exit_code == 0, result.diagnostics
    assert not clone.exists()
    (bundle,) = (root / ".onex_state/retired-repository-bundles").glob("*.bundle")
    restored = tmp_path / "restored"
    run_git(tmp_path, "clone", str(bundle), str(restored))
    assert run_git(restored, "rev-parse", "HEAD") == head
    assert (restored / "tracked").read_text() == "retired history\n"
    assert publisher.events[-1][1] == result


@pytest.mark.parametrize(
    "name",
    [
        *RETIRED_BY_RULING,
        "retired-fixture-one",
        "retired-fixture-two",
        "retired-fixture-three",
        "retired-fixture-four",
    ],
)
def test_each_contract_retirement_is_removed_and_repeat_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    root = setup_workspace(tmp_path, monkeypatch)
    if name not in RETIRED_BY_RULING:
        overlay = tmp_path / "overlays/node_host_reconcile_effect/overlay.yaml"
        overlay.parent.mkdir(parents=True)
        overlay.write_text(
            yaml.safe_dump({"retirement": {"additional_retired_repositories": [name]}})
        )
        monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path / "overlays"))
        assert set(load_retirement_policy().retired_repositories) >= {
            *RETIRED_BY_RULING,
            name,
        }
    clone = retired_clone(root, name)
    node = handler(RecordingEvaluator(), RecordingPublisher())
    assert node.handle(command(root)).exit_code == 0
    assert not clone.exists()
    assert node.handle(command(root)).exit_code == 0
    assert (
        len(list((root / ".onex_state/retired-repository-bundles").glob("*.bundle")))
        == 1
    )
    assert (root / "omnimarket/.git").is_dir()


def test_retirement_policy_cannot_drop_a_retired_repository() -> None:
    assert set(load_retirement_policy().retired_repositories) >= set(RETIRED_BY_RULING)


def test_check_reports_retired_clone_without_writing_a_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = setup_workspace(tmp_path, monkeypatch)
    clone = retired_clone(root)
    result = handler(RecordingEvaluator(), RecordingPublisher()).handle(
        command(root, "check")
    )
    assert result.exit_code == 2
    assert clone.is_dir()
    assert not (root / ".onex_state/retired-repository-bundles").exists()
    assert any(
        s.surface == "retired-clone:omnibase_compat" and s.verdict == "DID_NOT_MOVE"
        for s in result.surfaces
    )


@pytest.mark.parametrize(
    "kind",
    ["tracked", "untracked", "ignored", "linked-worktree", "not-a-clone", "symlink"],
)
def test_unbundled_work_is_never_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    root = setup_workspace(tmp_path, monkeypatch)
    clone = retired_clone(root)
    if kind == "tracked":
        (clone / "tracked").write_text("uncommitted work\n")
    elif kind == "untracked":
        (clone / "untracked").write_text("work\n")
    elif kind == "ignored":
        (clone / ".git/info/exclude").write_text("ignored\n")
        (clone / "ignored").write_text("work\n")
    elif kind == "linked-worktree":
        run_git(clone, "worktree", "add", "--detach", str(tmp_path / "peer"))
    elif kind == "not-a-clone":
        run_git(clone, "mv", "tracked", "kept")
        shutil.rmtree(clone / ".git")
    elif kind == "symlink":
        outside = tmp_path / "outside"
        clone.rename(outside)
        clone.symlink_to(outside, target_is_directory=True)
    result = handler(RecordingEvaluator(), RecordingPublisher()).handle(command(root))
    assert result.exit_code == 2, result.diagnostics
    assert clone.exists()
    assert any(
        s.surface == "retired-clone:omnibase_compat" and s.verdict == "UNHEALTHY"
        for s in result.surfaces
    )
    assert not list(root.glob(".onex_state/retired-repository-bundles/*.bundle"))


@pytest.mark.parametrize("operation", ["create", "verify"])
def test_failed_bundle_preserves_clone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    root = setup_workspace(tmp_path, monkeypatch)
    clone = retired_clone(root)
    original = _ReconcilePass.command

    def fail_bundle(self, args, **kwargs):
        if "bundle" in args and operation in args:
            return subprocess.CompletedProcess(args, 1, "", "fixture bundle failure")
        return original(self, args, **kwargs)

    monkeypatch.setattr(_ReconcilePass, "command", fail_bundle)
    result = handler(RecordingEvaluator(), RecordingPublisher()).handle(command(root))
    assert result.exit_code == 2
    assert clone.exists()
    assert any("fixture bundle failure" in s.detail for s in result.surfaces)
    assert not list(root.glob(".onex_state/retired-repository-bundles/*.bundle"))


def test_failed_backup_persistence_preserves_clone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = setup_workspace(tmp_path, monkeypatch)
    clone = retired_clone(root)

    def failed_sync(fd: int) -> None:
        raise OSError("fixture disk failure")

    monkeypatch.setattr(effect.os, "fsync", failed_sync)
    result = handler(RecordingEvaluator(), RecordingPublisher()).handle(command(root))
    assert result.exit_code == 2
    assert clone.exists()
    assert any("fixture disk failure" in surface.detail for surface in result.surfaces)


def test_clone_changed_during_backup_is_retained(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = setup_workspace(tmp_path, monkeypatch)
    clone = retired_clone(root)
    original = _ReconcilePass.command

    def concurrent_edit(self, args, **kwargs):
        result = original(self, args, **kwargs)
        if "bundle" in args and "verify" in args:
            (clone / "tracked").write_text("concurrent work\n")
        return result

    monkeypatch.setattr(_ReconcilePass, "command", concurrent_edit)
    result = handler(RecordingEvaluator(), RecordingPublisher()).handle(command(root))
    assert result.exit_code == 2
    assert (clone / "tracked").read_text() == "concurrent work\n"
    assert any(
        "clone changed during backup" in surface.detail for surface in result.surfaces
    )
    assert len(list(root.glob(".onex_state/retired-repository-bundles/*.bundle"))) == 1


def test_shallow_clone_is_retained(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = setup_workspace(tmp_path, monkeypatch)
    clone = retired_clone(root)
    (clone / ".git/shallow").write_text(run_git(clone, "rev-parse", "HEAD") + "\n")
    assert run_git(clone, "rev-parse", "--is-shallow-repository") == "true"
    result = handler(RecordingEvaluator(), RecordingPublisher()).handle(command(root))
    assert result.exit_code == 2
    assert clone.exists()
    assert any("shallow history" in surface.detail for surface in result.surfaces)


def test_detached_head_tags_and_other_branches_survive_in_the_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = setup_workspace(tmp_path, monkeypatch)
    clone = retired_clone(root)
    first = run_git(clone, "rev-parse", "HEAD")
    run_git(clone, "tag", "kept-tag")
    run_git(clone, "branch", "kept-branch")
    run_git(clone, "checkout", "--detach")
    (clone / "tracked").write_text("detached work\n")
    run_git(clone, "commit", "-am", "detached commit")
    detached = run_git(clone, "rev-parse", "HEAD")
    result = handler(RecordingEvaluator(), RecordingPublisher()).handle(command(root))
    assert result.exit_code == 0, result.diagnostics
    assert not clone.exists()
    (bundle,) = root.glob(".onex_state/retired-repository-bundles/*.bundle")
    restored = tmp_path / "restored"
    run_git(tmp_path, "clone", str(bundle), str(restored))
    assert run_git(restored, "rev-parse", "kept-tag") == first
    assert run_git(restored, "rev-parse", "origin/kept-branch") == first
    run_git(restored, "checkout", "--detach", detached)
    assert (restored / "tracked").read_text() == "detached work\n"


@pytest.mark.parametrize("relative", ["omnibase_compat/bundles", "../outside"])
def test_unsafe_backup_destination_preserves_clone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, relative: str
) -> None:
    root = setup_workspace(tmp_path, monkeypatch)
    clone = retired_clone(root)
    policy = load_retirement_policy().model_copy(update={"backup_directory": relative})
    monkeypatch.setattr(effect, "load_retirement_policy", lambda: policy)
    result = handler(RecordingEvaluator(), RecordingPublisher()).handle(command(root))
    assert result.exit_code == 2
    assert clone.exists()
    assert not list(root.rglob("*.bundle"))


@pytest.mark.parametrize(
    ("relative", "text"),
    [
        (
            "scripts/sync-clones.sh",
            "git clone git@github.com:OmniNode-ai/onex_change_control.git\n",
        ),
        ("systemd/sync.service", "[Service]\nExecStart=sync onex_change_control\n"),
        ("launchd/sync.sh", "clone onex_change_control\n"),
        ("deploy/sync.sh", "clone onex_change_control\n"),
        ("src/pkg/clone_sync.py", 'TARGET = "OmniNode-ai/onex_change_control"\n'),
        ("src/pkg/watch.py", 'LANDING_REPOS = frozenset({"onex_change_control"})\n'),
        (
            "src/pkg/watch.py",
            'LANDING_REPOS: tuple[str, ...] = ("onex_change_control",)\n',
        ),
        (
            "src/pkg/watch.py",
            'LANDING_REPOS = []\nLANDING_REPOS += ["onex_change_control"]\n',
        ),
        (
            "src/pkg/watch.py",
            'LANDING_REPOS = []\nLANDING_REPOS.append("onex_change_control")\n',
        ),
        (
            "src/pkg/watch.py",
            'LANDING_REPOS = set()\nLANDING_REPOS.update(["onex_change_control"])\n',
        ),
        (
            "config/hosts.yaml",
            "hosts:\n  host:\n    repos: [OmniNode-ai/onex_change_control]\n",
        ),
        (
            "config/hosts.json",
            '{"hosts": {"host": {"repos": ["onex_change_control"]}}}',
        ),
        ("repos.yaml", "repositories:\n- name: onex_change_control\n"),
    ],
)
def test_registry_wide_refusal_of_retired_targets(
    tmp_path: Path, relative: str, text: str
) -> None:
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    findings = retired_repository_findings(tmp_path, load_retirement_policy())
    assert findings
    assert {f.repository for f in findings} == {"onex_change_control"}
    assert {f.path for f in findings} == {relative}


def test_comments_and_historical_classifications_are_not_sync_targets(
    tmp_path: Path,
) -> None:
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/sync.sh").write_text(
        "# onex_change_control is retired\nclone omnidash\n"
    )
    (tmp_path / "src/pkg").mkdir(parents=True)
    (tmp_path / "src/pkg/sync.py").write_text(
        '"""Replaces onex_change_control."""\nREPOS = ["omnidash", "onex_change_control_v2", "onex_change_control.v2"]\n'
    )
    (tmp_path / "config").mkdir()
    (tmp_path / "config/history.yaml").write_text(
        "companion_repositories: [onex_change_control]\n"
    )
    assert retired_repository_findings(tmp_path, load_retirement_policy()) == ()


@pytest.mark.parametrize(
    "constant",
    [
        "DEFAULT_REPOS",
        "_DEFAULT_REPOS",
        "_DEFAULT_SCAN_REPOS",
        "KNOWN_REPOS",
        "_KNOWN_REPOS",
        "REGISTRY_REPOS",
        "FLEET_REPOS",
        "_CANONICAL_REPOS",
        "LANDING_REPOS",
        "SIBLING_CLONE_MANIFEST",
    ],
)
def test_operational_registry_constants_are_all_checked(
    tmp_path: Path, constant: str
) -> None:
    source = tmp_path / "src/pkg/registry.py"
    source.parent.mkdir(parents=True)
    source.write_text(f'{constant} = ["omnibase_compat"]\n')
    findings = retired_repository_findings(tmp_path, load_retirement_policy())
    assert {finding.repository for finding in findings} == {"omnibase_compat"}


@pytest.mark.parametrize(
    "relative", ["scripts/sync.sh", "src/pkg/registry.py", "config/hosts.yaml"]
)
def test_symbolic_sync_or_registry_inputs_cannot_hide_retired_targets(
    tmp_path: Path, relative: str
) -> None:
    target = tmp_path / "target"
    target.write_text("onex_change_control\n")
    link = tmp_path / relative
    link.parent.mkdir(parents=True)
    link.symlink_to(target)
    with pytest.raises(ValueError, match="symbolic"):
        retired_repository_findings(tmp_path, load_retirement_policy())


def test_contract_declares_the_generated_environment_marker() -> None:
    policy = load_retirement_policy()
    assert policy.generated_directory_markers == ("pyvenv.cfg",)
    with pytest.raises(ValueError, match="generated_directory_markers"):
        policy.model_validate(
            {**policy.model_dump(), "generated_directory_markers": ()}
        )


def test_generated_virtualenv_symlinks_are_not_sync_inputs(tmp_path: Path) -> None:
    venv = tmp_path / "scripts/deploy-agent/.venv"
    (venv / "lib").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("home = /usr/bin\n")
    (venv / "lib64").symlink_to("lib")
    (venv / "lib/site.py").write_text("onex_change_control\n")
    assert retired_repository_findings(tmp_path, load_retirement_policy()) == ()


def test_symlink_beside_a_virtualenv_is_still_refused(tmp_path: Path) -> None:
    venv = tmp_path / "scripts/deploy-agent/.venv"
    (venv / "lib").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("home = /usr/bin\n")
    (venv / "lib64").symlink_to("lib")
    (venv / "lib/site.py").write_text("onex_change_control\n")
    target = tmp_path / "target"
    target.write_text("onex_change_control\n")
    (tmp_path / "scripts/deploy-agent/link.sh").symlink_to(target)
    with pytest.raises(ValueError, match="symbolic"):
        retired_repository_findings(tmp_path, load_retirement_policy())


def test_venv_without_marker_is_still_refused(tmp_path: Path) -> None:
    venv = tmp_path / "scripts/deploy-agent/.venv"
    (venv / "lib").mkdir(parents=True)
    (venv / "lib64").symlink_to("lib")
    (venv / "lib/site.py").write_text("onex_change_control\n")
    with pytest.raises(ValueError, match="symbolic"):
        retired_repository_findings(tmp_path, load_retirement_policy())


def test_host_reconcile_ignores_a_generated_virtualenv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = setup_workspace(tmp_path, monkeypatch)
    venv = root / "omnibase_infra/scripts/deploy-agent/.venv"
    (venv / "lib").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("home = /usr/bin\n")
    (venv / "lib64").symlink_to("lib")
    (venv / "lib/site.py").write_text("onex_change_control\n")
    result = handler(RecordingEvaluator(), RecordingPublisher()).handle(command(root))
    assert result.exit_code == 0, result.diagnostics
    assert not any(s.surface == "retired-repository-targets" for s in result.surfaces)


def test_host_refuses_retired_target_next_to_a_virtualenv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = setup_workspace(tmp_path, monkeypatch)
    venv = root / "omnibase_infra/scripts/deploy-agent/.venv"
    (venv / "lib").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("home = /usr/bin\n")
    (venv / "lib64").symlink_to("lib")
    (venv / "lib/site.py").write_text("onex_change_control\n")
    (root / "omnibase_infra/scripts/sync.sh").write_text("clone onex_change_control\n")
    result = handler(RecordingEvaluator(), RecordingPublisher()).handle(command(root))
    assert result.exit_code == 2
    assert any(s.surface == "retired-repository-targets" for s in result.surfaces)


def test_host_refuses_a_retired_target_in_a_sibling_before_sync_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = setup_workspace(tmp_path, monkeypatch)
    sibling = root / "omnimarket/scripts"
    sibling.mkdir()
    (sibling / "sync.sh").write_text("clone onex_change_control\n")
    result = handler(RecordingEvaluator(), RecordingPublisher()).handle(command(root))
    assert result.exit_code == 2
    assert any(s.surface == "retired-repository-targets" for s in result.surfaces)
    assert not (root / "verifier.calls").exists(), (
        "refusal precedes clone and venv delegates"
    )
    assert not (root / ".onex-reconcile-host.lock").exists()


def test_stale_sync_manifest_cannot_recreate_the_archived_retired_clone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = setup_workspace(tmp_path, monkeypatch)
    clone = retired_clone(root)
    manifest = root / "omnibase_infra/scripts/runtime_build/sibling_clone_manifest.sh"
    manifest.write_text(
        manifest.read_text().replace(
            "SIBLING_CLONE_MANIFEST=(omnibase_infra omnimarket)",
            "SIBLING_CLONE_MANIFEST=(omnibase_infra omnimarket omnibase_compat)",
        )
    )
    result = handler(RecordingEvaluator(), RecordingPublisher()).handle(command(root))
    assert result.exit_code == 2
    assert not clone.exists()
    assert len(list(root.glob(".onex_state/retired-repository-bundles/*.bundle"))) == 1
    assert not (root / "verifier.calls").exists()
    assert any(s.surface == "retired-repository-targets" for s in result.surfaces)


def test_retirement_inmemory_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = setup_workspace(tmp_path, monkeypatch)
    clone = retired_clone(root)
    bus = EventBusInmemory(environment="test", group="retirement")
    received: list[ModelHostReconcileRunResult] = []

    async def observe(message: ModelEventMessage) -> None:
        received.append(ModelHostReconcileRunResult.model_validate_json(message.value))

    async def start() -> None:
        await bus.start()
        await bus.subscribe(
            publish_topics()[1], on_message=observe, group_id="retirement-proof"
        )

    class InmemoryPublisher:
        def publish(self, topic: str, event: BaseModel) -> None:
            asyncio.run(bus.publish(topic, None, event.model_dump_json().encode()))

    asyncio.run(start())
    try:
        result = handler(RecordingEvaluator(), InmemoryPublisher()).handle(
            command(root)
        )
    finally:
        asyncio.run(bus.close())
    assert result.exit_code == 0
    assert not clone.exists()
    assert received == [result]
    assert any(
        s.surface == "retired-clone:omnibase_compat" and s.verdict == "MOVED"
        for s in received[0].surfaces
    )


def test_checkout_has_no_retired_sync_or_registry_targets() -> None:
    assert (
        retired_repository_findings(
            Path(__file__).resolve().parents[1], load_retirement_policy()
        )
        == ()
    )


def test_retirement_falsifier_is_wired_to_ci_and_precommit() -> None:
    root = Path(__file__).resolve().parents[1]
    filename = "tests/test_retired_repository_reconcile.py"
    ci = yaml.safe_load((root / ".github/workflows/ci.yml").read_text())
    assert any(
        filename in str(step.get("run", ""))
        for job in ci["jobs"].values()
        for step in job.get("steps", [])
    )
    hooks = yaml.safe_load((root / ".pre-commit-config.yaml").read_text())
    assert any(
        filename in h.get("entry", "") for repo in hooks["repos"] for h in repo["hooks"]
    )


COMPOSE_WITH_TAGS = (
    "services:\n  qdrant:\n    ports: !override []\n    labels: !reset null\n"
)


def test_contract_declares_the_compose_tags() -> None:
    policy = load_retirement_policy()
    assert policy.compose_yaml_tags == ("override", "reset")
    with pytest.raises(ValueError, match="compose_yaml_tags"):
        policy.model_validate({**policy.model_dump(), "compose_yaml_tags": ()})


def test_compose_tag_file_is_read_without_a_finding(tmp_path: Path) -> None:
    compose = tmp_path / "docker-compose.prod.yml"
    compose.write_text(COMPOSE_WITH_TAGS)
    before = compose.read_bytes()
    assert retired_repository_findings(tmp_path, load_retirement_policy()) == ()
    assert compose.read_bytes() == before


def test_compose_tag_file_still_reports_a_retired_target(tmp_path: Path) -> None:
    (tmp_path / "docker-compose.prod.yml").write_text(
        COMPOSE_WITH_TAGS + "repos: [onex_change_control]\n"
    )
    findings = retired_repository_findings(tmp_path, load_retirement_policy())
    assert [f.repository for f in findings] == ["onex_change_control"]


@pytest.mark.parametrize(
    "text",
    ["services:\n  q:\n    ports: !undeclared []\n", "services: [unclosed\n"],
    ids=["undeclared_tag", "invalid_yaml"],
)
def test_undeclared_tag_or_invalid_yaml_is_still_malformed(
    tmp_path: Path, text: str
) -> None:
    (tmp_path / "docker-compose.prod.yml").write_text(text)
    with pytest.raises(ValueError, match="malformed registry configuration"):
        retired_repository_findings(tmp_path, load_retirement_policy())


def test_compose_tag_workspace_exits_zero_then_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = setup_workspace(tmp_path, monkeypatch)
    compose = root / "omnibase_infra/docker-compose.prod.yml"
    compose.write_text(COMPOSE_WITH_TAGS)
    result = handler(RecordingEvaluator(), RecordingPublisher()).handle(command(root))
    assert result.exit_code == 0, result.diagnostics
    assert not any(s.surface == "retired-repository-targets" for s in result.surfaces)
    compose.write_text(COMPOSE_WITH_TAGS + "repos: [onex_change_control]\n")
    result = handler(RecordingEvaluator(), RecordingPublisher()).handle(command(root))
    assert result.exit_code == 2
    assert any(s.surface == "retired-repository-targets" for s in result.surfaces)
