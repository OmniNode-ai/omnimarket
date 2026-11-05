# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Sparse replay of the recorded retained checkout and shared cache lengths."""

from pathlib import Path

import pytest

from omnimarket.nodes.node_runner_workspace_retention_effect.models.model_runner_retention import (
    ModelRunnerRetentionPolicy,
    ModelRunnerRetentionRequest,
)
from omnimarket.nodes.node_runner_workspace_retention_effect.node import (
    NodeRunnerWorkspaceRetentionEffect,
)

RECORDED_WORKSPACE_BYTES = 6_593_306_624
RECORDED_CACHE_BYTES = {
    ".uv-cache": 2_559_918_080,
    "_tool": 1_740_861_440,
    ".pnpm-store": 692_617_216,
}


@pytest.fixture
def replay(tmp_path: Path) -> ModelRunnerRetentionRequest:
    runner = tmp_path / "runner"
    work = runner / "_work"
    incoming = work / "incoming" / "incoming"
    completed = work / "completed" / "completed"
    incoming.mkdir(parents=True)
    completed.mkdir(parents=True)
    (incoming / "stale").write_text("reset positive control")
    (runner / ".runner").write_text("runner control sentinel")
    remainder = RECORDED_WORKSPACE_BYTES - sum(RECORDED_CACHE_BYTES.values())
    for directory, length in [(completed, remainder)] + [
        (work / name, length) for name, length in RECORDED_CACHE_BYTES.items()
    ]:
        directory.mkdir(exist_ok=True)
        with (directory / "retained").open("wb") as stream:
            stream.truncate(length)
    return ModelRunnerRetentionRequest(
        runner_home=str(runner),
        workspace=str(incoming),
        phase="job_started",
        lifecycle_boundary=True,
        dry_run=False,
        policy=ModelRunnerRetentionPolicy(
            work_directory="_work",
            cache_budgets=dict.fromkeys(RECORDED_CACHE_BYTES, 0),
            protected_directories=("_actions", "_temp", "_PipelineMapping"),
        ),
    )


def retained_bytes(work: Path) -> int:
    return sum(path.stat().st_size for path in work.rglob("*") if path.is_file())


def test_incoming_reset_and_runner_control_preserved(
    replay: ModelRunnerRetentionRequest,
) -> None:
    result = NodeRunnerWorkspaceRetentionEffect().handle(replay)
    assert result.status == "completed", result
    assert not (Path(replay.workspace) / "stale").exists()
    assert (
        Path(replay.runner_home) / ".runner"
    ).read_text() == "runner control sentinel"


def test_completed_checkout_does_not_survive_next_job(
    replay: ModelRunnerRetentionRequest,
) -> None:
    NodeRunnerWorkspaceRetentionEffect().handle(replay)
    assert not (Path(replay.runner_home) / "_work/completed").exists()


def test_recorded_shared_cache_bytes_are_bounded(
    replay: ModelRunnerRetentionRequest,
) -> None:
    NodeRunnerWorkspaceRetentionEffect().handle(replay)
    assert retained_bytes(Path(replay.runner_home) / "_work") == 0


def test_recorded_bytes_are_measured_and_reclaimed(
    replay: ModelRunnerRetentionRequest,
) -> None:
    incoming_bytes = (Path(replay.workspace) / "stale").stat().st_size
    result = NodeRunnerWorkspaceRetentionEffect().handle(replay)
    assert result.bytes_before == RECORDED_WORKSPACE_BYTES + incoming_bytes
    assert result.bytes_after == 0
    assert result.bytes_removed == result.bytes_before
    assert set(result.removed) == {"incoming", "completed", *RECORDED_CACHE_BYTES}
    assert Path(replay.workspace).is_dir()
    again = NodeRunnerWorkspaceRetentionEffect().handle(replay)
    assert again.status == "completed"
    assert again.bytes_removed == 0


@pytest.mark.parametrize(
    "update",
    [{"lifecycle_boundary": False}, {"active_workspaces": ("any-active-workspace",)}],
)
def test_unverified_boundary_or_any_active_job_refuses_without_cleanup(
    replay: ModelRunnerRetentionRequest,
    update: dict[str, object],
) -> None:
    before = retained_bytes(Path(replay.runner_home) / "_work")
    result = NodeRunnerWorkspaceRetentionEffect().handle(
        replay.model_copy(update=update)
    )
    assert result.status == "refused"
    assert retained_bytes(Path(replay.runner_home) / "_work") == before
    assert result.bytes_removed is None


def test_completed_boundary_does_not_recreate_checkout(
    replay: ModelRunnerRetentionRequest,
) -> None:
    result = NodeRunnerWorkspaceRetentionEffect().handle(
        replay.model_copy(update={"phase": "job_completed"})
    )
    assert result.status == "completed"
    assert not Path(replay.workspace).exists()
    assert result.bytes_after == 0


def test_dry_run_measures_but_removes_nothing(
    replay: ModelRunnerRetentionRequest,
) -> None:
    result = NodeRunnerWorkspaceRetentionEffect().handle(
        replay.model_copy(update={"dry_run": True})
    )
    assert result.status == "completed"
    assert result.bytes_before == result.bytes_after
    assert result.bytes_removed == 0
    assert result.removed == ()
    assert set(result.planned) == {"incoming", "completed", *RECORDED_CACHE_BYTES}
    assert (Path(replay.workspace) / "stale").is_file()


def test_git_checkouts_are_disposable_but_controls_and_unknown_data_survive(
    replay: ModelRunnerRetentionRequest,
) -> None:
    work = Path(replay.runner_home) / "_work"
    (work / "completed/completed/.git").mkdir()
    (work / "completed/completed/.git/HEAD").write_text("fixture git metadata")
    for name in ("_actions", "_temp", "_PipelineMapping", "unknown", ".credentials"):
        (work / name).mkdir()
        (work / name / "sentinel").write_text(name)
    result = NodeRunnerWorkspaceRetentionEffect().handle(replay)
    assert result.status == "completed"
    assert not (work / "completed").exists()
    for name in ("_actions", "_temp", "_PipelineMapping", "unknown", ".credentials"):
        assert (work / name / "sentinel").read_text() == name
        assert name in result.preserved


def test_budget_is_inclusive_and_supplied_by_overlay(
    replay: ModelRunnerRetentionRequest,
) -> None:
    assert replay.policy is not None
    budgets = dict(RECORDED_CACHE_BYTES)
    budgets[".pnpm-store"] -= 1
    policy = replay.policy.model_copy(update={"cache_budgets": budgets})
    result = NodeRunnerWorkspaceRetentionEffect().handle(
        replay.model_copy(update={"policy": policy})
    )
    assert result.status == "completed"
    assert ".pnpm-store" in result.removed
    assert ".uv-cache" in result.preserved
    assert "_tool" in result.preserved
    assert (
        result.bytes_after
        == RECORDED_CACHE_BYTES[".uv-cache"] + RECORDED_CACHE_BYTES["_tool"]
    )


def test_symlink_targets_survive_inside_and_outside_candidates(
    replay: ModelRunnerRetentionRequest, tmp_path: Path
) -> None:
    work = Path(replay.runner_home) / "_work"
    external = tmp_path / "external"
    external.mkdir()
    (external / "sentinel").write_text("external")
    (work / "linked").symlink_to(external, target_is_directory=True)
    (work / "completed/completed/link").symlink_to(external, target_is_directory=True)
    result = NodeRunnerWorkspaceRetentionEffect().handle(replay)
    assert result.status == "completed"
    assert (external / "sentinel").read_text() == "external"
    assert (work / "linked").is_symlink()
    assert "linked" in result.preserved


@pytest.mark.parametrize(
    "path", ["/", "relative/repo", "../repo", "_temp/_temp", "incoming/other"]
)
def test_unsafe_workspace_refuses_all_cleanup(
    replay: ModelRunnerRetentionRequest, path: str
) -> None:
    workspace = (
        path
        if path.startswith("/") or path.startswith("relative")
        else str(Path(replay.runner_home) / "_work" / path)
    )
    result = NodeRunnerWorkspaceRetentionEffect().handle(
        replay.model_copy(update={"workspace": workspace})
    )
    assert result.status == "refused"
    assert (Path(replay.runner_home) / "_work/completed").exists()


@pytest.mark.parametrize(
    "name", ["../escape", "/absolute", ".", "..", "_temp", "_actions", ".credentials"]
)
def test_unsafe_cache_declaration_refuses(
    replay: ModelRunnerRetentionRequest, name: str
) -> None:
    assert replay.policy is not None
    policy = replay.policy.model_copy(update={"cache_budgets": {name: 0}})
    result = NodeRunnerWorkspaceRetentionEffect().handle(
        replay.model_copy(update={"policy": policy})
    )
    assert result.status == "refused"
    assert (Path(replay.runner_home) / "_work/completed").exists()


def test_runner_identity_is_required(replay: ModelRunnerRetentionRequest) -> None:
    (Path(replay.runner_home) / ".runner").unlink()
    result = NodeRunnerWorkspaceRetentionEffect().handle(replay)
    assert result.status == "refused"
    assert (Path(replay.runner_home) / "_work/completed").exists()


def test_symlink_root_is_refused(replay: ModelRunnerRetentionRequest) -> None:
    home = Path(replay.runner_home)
    work = home / "_work"
    original = home / "work-original"
    work.rename(original)
    work.symlink_to(original, target_is_directory=True)
    result = NodeRunnerWorkspaceRetentionEffect().handle(replay)
    assert result.status == "refused"
    assert (original / "completed").exists()


def test_concurrent_callback_returns_locked(
    replay: ModelRunnerRetentionRequest,
) -> None:
    import fcntl

    with (Path(replay.runner_home) / ".workspace-retention.lock").open("w") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = NodeRunnerWorkspaceRetentionEffect().handle(replay)
    assert result.status == "locked"
    assert (Path(replay.runner_home) / "_work/completed").exists()


def test_packaged_policy_loads_through_declared_overlay(
    replay: ModelRunnerRetentionRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    node = NodeRunnerWorkspaceRetentionEffect()
    monkeypatch.setenv(
        "RUNNER_RETENTION_POLICY_FILE",
        str(node.contract_path.with_name("overlay.yaml")),
    )
    result = node.handle(replay.model_copy(update={"policy": None}))
    assert result.status == "completed"
    assert result.bytes_after == 0


@pytest.mark.asyncio
async def test_contract_bus_dispatch_reclaims_recorded_case(
    replay: ModelRunnerRetentionRequest,
    tmp_path: Path,
) -> None:
    from uuid import uuid4

    import yaml
    from omnibase_infra.runtime.auto_wiring.discovery import (
        discover_contracts_from_paths,
    )

    from omnimarket.adapters.codex.local_runtime_dispatch import LocalRuntimeDispatch
    from omnimarket.adapters.codex.runtime_client import (
        ModelDispatchBusCommand,
        default_command_topic,
        default_response_topic,
    )
    from omnimarket.nodes.node_runner_workspace_retention_effect.models.model_runner_retention import (
        ModelRunnerRetentionResult,
    )

    contract = NodeRunnerWorkspaceRetentionEffect.contract_path
    manifest = discover_contracts_from_paths([contract])
    assert not manifest.errors
    assert len(manifest.contracts) == 1
    raw = yaml.safe_load(contract.read_text())
    assert "event_bus" not in raw
    assert manifest.contracts[0].event_bus is None
    correlation_id = uuid4()
    runtime = LocalRuntimeDispatch(
        adapter_command_topic=default_command_topic(),
        state_root=tmp_path / "runtime-control",
    )
    terminal, evidence = await runtime.dispatch(
        ModelDispatchBusCommand(
            command_name="node_runner_workspace_retention_effect.retain_runner_workspace",
            requester="runner-lifecycle-replay",
            correlation_id=correlation_id,
            payload=replay.model_dump(mode="json"),
            response_topic=default_response_topic(),
            timeout_seconds=30,
        )
    )
    assert terminal.status == "completed", terminal.error_message
    assert terminal.correlation_id == correlation_id
    assert evidence.runtime_observation.status == "OBSERVED"
    assert evidence.adapter_dispatch_binding.node_contract.status == "RESOLVED"
    assert evidence.command_topic == raw["runtime_dispatch"]["command_topic"]
    assert (
        evidence.terminal_topic == raw["runtime_dispatch"]["terminal_events"]["success"]
    )
    result = ModelRunnerRetentionResult.model_validate(terminal.payload)
    assert result.status == "completed"
    assert result.bytes_after == 0
    assert result.bytes_removed is not None
    assert result.bytes_removed >= RECORDED_WORKSPACE_BYTES
    assert not (Path(replay.runner_home) / "_work/completed").exists()
    assert (
        Path(replay.runner_home) / ".runner"
    ).read_text() == "runner control sentinel"


def test_nested_mount_refuses_before_removing_any_data(
    replay: ModelRunnerRetentionRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnimarket.nodes.node_runner_workspace_retention_effect.handlers import (
        handler_runner_retention as handler,
    )

    work = Path(replay.runner_home) / "_work"
    monkeypatch.setattr(
        handler, "_mount_points", lambda: (work / "completed/completed/mounted",)
    )
    result = NodeRunnerWorkspaceRetentionEffect().handle(replay)
    assert result.status == "refused"
    assert "nested mount" in result.reason
    assert (work / ".uv-cache/retained").is_file()
    assert (work / "completed").exists()


def test_runtime_inside_disposable_checkout_is_refused(
    replay: ModelRunnerRetentionRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnimarket.nodes.node_runner_workspace_retention_effect.handlers import (
        handler_runner_retention as handler,
    )

    monkeypatch.setattr(
        handler, "__file__", str(Path(replay.workspace) / "node/handler.py")
    )
    result = NodeRunnerWorkspaceRetentionEffect().handle(replay)
    assert result.status == "refused"
    assert "runtime must be installed outside" in result.reason
    assert (Path(replay.runner_home) / "_work/completed").exists()


def test_partial_deletion_reports_measured_bytes(
    replay: ModelRunnerRetentionRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnimarket.nodes.node_runner_workspace_retention_effect.handlers import (
        handler_runner_retention as handler,
    )

    work = Path(replay.runner_home) / "_work"
    original = handler.shutil.rmtree

    def fail_after_one_file(path: str, *, dir_fd: int) -> None:
        (work / path / "retained").unlink()
        raise PermissionError("fixture permission failure after partial removal")

    fail_after_one_file.avoids_symlink_attacks = original.avoids_symlink_attacks
    monkeypatch.setattr(handler.shutil, "rmtree", fail_after_one_file)
    result = NodeRunnerWorkspaceRetentionEffect().handle(replay)
    assert result.status == "failed"
    assert result.reason == "fixture permission failure after partial removal"
    assert result.bytes_removed == RECORDED_CACHE_BYTES[".pnpm-store"]
    assert result.bytes_after == retained_bytes(work)
    assert result.removed == ()
    assert (work / "completed").exists()


def test_workspace_recreation_never_follows_changed_work_root(
    replay: ModelRunnerRetentionRequest, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from omnimarket.nodes.node_runner_workspace_retention_effect.handlers import (
        handler_runner_retention as handler,
    )

    work = Path(replay.runner_home) / "_work"
    external = tmp_path / "external-after-swap"
    external.mkdir()
    (external / "sentinel").write_text("outside")
    original = handler.HandlerRunnerRetention._prepare_workspace

    def swap_root(work_fd: int, repository: str) -> None:
        work.rename(work.with_name("original-work"))
        work.symlink_to(external, target_is_directory=True)
        original(work_fd, repository)

    monkeypatch.setattr(
        handler.HandlerRunnerRetention, "_prepare_workspace", staticmethod(swap_root)
    )
    result = NodeRunnerWorkspaceRetentionEffect().handle(replay)
    assert result.status == "failed"
    assert result.bytes_after is None
    assert result.bytes_removed is None
    assert not (external / "incoming").exists()
    assert (external / "sentinel").read_text() == "outside"


def test_repeated_repository_changes_do_not_accumulate(
    replay: ModelRunnerRetentionRequest,
) -> None:
    node = NodeRunnerWorkspaceRetentionEffect()
    first = node.handle(replay)
    assert first.status == "completed"
    work = Path(replay.runner_home) / "_work"
    for repository in ("second", "third", "fourth"):
        checkout = work / repository / repository
        checkout.mkdir(parents=True)
        (checkout / "payload").write_text(repository)
        cache = work / ".uv-cache"
        cache.mkdir()
        (cache / "payload").write_text(repository)
        result = node.handle(replay.model_copy(update={"workspace": str(checkout)}))
        assert result.status == "completed"
        assert result.bytes_after == 0
        assert result.bytes_removed is not None
        assert result.bytes_removed > 0
