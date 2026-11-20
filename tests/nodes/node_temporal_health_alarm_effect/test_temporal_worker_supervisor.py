# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One persistent worker unit serves the workflow 1, reader, closer and prune task queues (OMN-24901).

The unit's runtime starts one child worker per declared task queue from the checkout the unit names, and ends them all
when any one ends, so the unit's restart policy starts every queue again. A declared worker whose module the checkout does
not carry is left out and named in the start line; the primary worker (workflow 1) is always required.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

from omnimarket.nodes.node_temporal_health_alarm_effect import (
    runtime_temporal_worker as worker,
)

PRIMARY = "omnimarket.nodes.node_prove_running_workflow"
READER = "omnimarket.nodes.node_merged_change_runner_read_workflow.runtime_runner_read_worker"
CLOSER = "omnimarket.nodes.node_sprint_closer_effect"
PRUNE = "omnimarket.nodes.node_worktree_prune_temporal_effect"
QUEUES = {
    "onex-merged-change-runner-read",
    "onex-sprint-closer",
    "onex-worktree-prune",
}


def _carry(project: Path, module: str) -> None:
    """Make ``project`` carry ``module`` the way a checkout does: a package directory, or a module file."""
    parts = module.split(".")
    base = project / "src" / Path(*parts[:-1])
    base.mkdir(parents=True, exist_ok=True)
    if parts[-1].startswith("runtime_"):
        (base / f"{parts[-1]}.py").write_text("", encoding="utf-8")
    else:
        (base / parts[-1]).mkdir(exist_ok=True)


def _project(tmp_path: Path, *modules: str) -> Path:
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    for module in modules:
        _carry(tmp_path, module)
    return tmp_path


def _settings(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    monkeypatch.delenv("ONEX_TEMPORAL_ADDRESS", raising=False)
    monkeypatch.delenv("ONEX_TEMPORAL_NAMESPACE", raising=False)
    return worker.worker_settings()


@pytest.mark.unit
def test_the_contract_declares_the_reader_closer_and_prune_queues() -> None:
    declared = worker.worker_settings()["also_serves"]
    assert {str(entry["task_queue"]) for entry in declared} == QUEUES
    assert {str(entry["module"]) for entry in declared} == {READER, CLOSER, PRUNE}


@pytest.mark.unit
def test_one_command_per_declared_worker_the_checkout_carries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path, PRIMARY, READER, CLOSER, PRUNE)
    settings = _settings(monkeypatch)
    commands, skipped = worker.worker_commands(settings, project, "uv", "/omni")
    assert skipped == []
    assert [c[c.index("-m") + 1] for c in commands] == [PRIMARY, READER, CLOSER, PRUNE]
    for command in commands:
        assert command[:5] == ["uv", "run", "--frozen", "--project", str(project)]
    closer = commands[2]
    assert closer[closer.index("-m") + 2] == "worker"
    assert closer[closer.index("--target-host") + 1] == "127.0.0.1:7233"
    assert closer[closer.index("--omni-home") + 1] == "/omni"


@pytest.mark.unit
def test_a_worker_whose_module_the_checkout_lacks_is_left_out_and_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path, PRIMARY, CLOSER)
    commands, skipped = worker.worker_commands(
        _settings(monkeypatch), project, "uv", "/omni"
    )
    assert [c[c.index("-m") + 1] for c in commands] == [PRIMARY, CLOSER]
    assert sorted(skipped) == sorted([READER, PRUNE])


@pytest.mark.unit
def test_the_primary_worker_is_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path, READER, CLOSER, PRUNE)
    monkeypatch.setenv("ONEX_TEMPORAL_WORKER_PROJECT", str(project))
    with pytest.raises(ValueError, match="is not a checkout carrying"):
        worker.worker_project(_settings(monkeypatch))


@pytest.mark.unit
def test_the_probe_names_what_the_unit_would_serve(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = _project(tmp_path, PRIMARY, READER)
    monkeypatch.setenv("ONEX_TEMPORAL_WORKER_PROJECT", str(project))
    assert worker.main(["--omni-home", "/omni", "--state-dir", "/s", "--probe"]) == 0
    probe = json.loads(capsys.readouterr().out)
    assert probe["probe"] == "ok"
    assert probe["serves"] == [PRIMARY, READER]
    assert sorted(probe["skipped"]) == sorted([CLOSER, PRUNE])


def _sleeper() -> list[str]:
    return [sys.executable, "-c", "import time; time.sleep(120)"]


@pytest.mark.unit
def test_when_one_worker_exits_the_others_are_ended_and_its_code_is_returned() -> None:
    started = time.monotonic()
    code = worker.supervise(
        [_sleeper(), [sys.executable, "-c", "raise SystemExit(3)"], _sleeper()],
        {},
        poll_interval=0.05,
        grace=5.0,
    )
    assert code == 3
    assert time.monotonic() - started < 30


@pytest.mark.unit
def test_a_worker_that_exits_cleanly_still_ends_the_unit_with_a_failure() -> None:
    """Restart=on-failure starts the unit again only for a failure, and a worker is never meant to end."""
    code = worker.supervise(
        [_sleeper(), [sys.executable, "-c", "raise SystemExit(0)"]],
        {},
        poll_interval=0.05,
        grace=5.0,
    )
    assert code == 1
