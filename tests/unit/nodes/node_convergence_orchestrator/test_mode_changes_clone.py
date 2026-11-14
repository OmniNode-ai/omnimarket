# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Observe changes no clone and act changes one, on real git clones (OMN-24198).

The "host" here is a sender that runs the host command the overlay's argv names, in this process, against a planted registry:
``--observe`` runs the observe form, anything else the dirty-converge tick with a fake sync engine and a fake converge script
(the real handler, the real git reads). The orchestrator's handler, parser and overlay are the real ones.
"""

from __future__ import annotations

import datetime as dt
import os
import subprocess
import sys
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.handlers.schedule_gate import ScheduleGate
from omnimarket.models.model_convergence_orchestrator import (
    EnumConvergenceCloneStatus,
    EnumConvergenceHostOutcome,
    EnumConvergenceRunOutcome,
    ModelConvergenceOrchestratorOverlay,
    ModelConvergenceRunCompleted,
)
from omnimarket.models.model_lab_work_unit import (
    EnumLabWorkUnitStatus,
    ModelLabWorkUnitReceipt,
    ModelLabWorkUnitRequest,
)
from omnimarket.nodes.node_canonical_clone_dirty_converge_effect.handlers.handler_canonical_clone_dirty_converge_effect import (
    HandlerCanonicalCloneDirtyConvergeEffect,
)
from omnimarket.nodes.node_canonical_clone_dirty_converge_effect.handlers.observe_clones import (
    observe,
)
from omnimarket.nodes.node_convergence_orchestrator.handlers.handler_convergence_scheduled import (
    NODE_NAME,
    HandlerConvergenceScheduled,
)

pytestmark = pytest.mark.unit

NODE_DIR = (
    Path(__file__).resolve().parents[4] / "src" / "omnimarket" / "nodes" / NODE_NAME
)
T0 = dt.datetime(2026, 11, 14, 4, 0, tzinfo=dt.UTC)
FAKE_CONVERGE = """#!/bin/bash
clone="$1"
git -C "$clone" checkout --quiet -f main
git -C "$clone" reset --hard --quiet '@{u}'
"""


def git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
        env=scrub_git_location_env(os.environ),
    ).stdout.strip()


def tree_state(clone: Path) -> dict[str, bytes]:
    """Everything an act run could change in a clone, byte for byte: every working file, HEAD, branch heads, the index, the status."""
    state: dict[str, bytes] = {
        path.relative_to(clone).as_posix(): path.read_bytes()
        for path in sorted(clone.rglob("*"))
        if path.is_file() and ".git" not in path.relative_to(clone).parts
    }
    for name, args in {
        "@head": ("rev-parse", "HEAD"),
        "@branches": ("for-each-ref", "refs/heads", "refs/remotes"),
        "@staged": ("diff", "--cached", "--binary"),
        "@status": ("status", "--porcelain=v1", "-z", "--untracked-files=all"),
    }.items():
        state[name] = subprocess.run(
            ["git", "--git-dir", str(clone / ".git"), *args],
            capture_output=True,
            check=False,
            env=scrub_git_location_env(os.environ),
        ).stdout
    state["@index"] = (clone / ".git" / "index").read_bytes()
    return state


class Registry:
    """A registry root holding three clones of a remote with one newer commit upstream.

    ``parked`` sits on a lane branch with no local work, ``dirty`` holds a modified tracked file, ``clean`` is on main.
    """

    def __init__(self, tmp: Path) -> None:
        self.home = tmp / "registry"
        self.home.mkdir(parents=True)
        seed = tmp / "seed"
        seed.mkdir(parents=True)
        git("init", "--quiet", "-b", "main", cwd=seed)
        git("config", "user.email", "t@example.com", cwd=seed)
        git("config", "user.name", "T", cwd=seed)
        (seed / "a.txt").write_text("one\n")
        git("add", ".", cwd=seed)
        git("commit", "--quiet", "-m", "one", cwd=seed)
        remote = tmp / "remote.git"
        git("clone", "--quiet", "--bare", str(seed), str(remote), cwd=tmp)
        for name in ("clean", "dirty", "parked"):
            git(
                "clone",
                "--quiet",
                "-b",
                "main",
                str(remote),
                str(self.home / name),
                cwd=tmp,
            )
            git("config", "user.email", "t@example.com", cwd=self.home / name)
            git("config", "user.name", "T", cwd=self.home / name)
        git("remote", "add", "origin", str(remote), cwd=seed)
        (seed / "newer.txt").write_text("newer\n")
        git("add", "newer.txt", cwd=seed)
        git("commit", "--quiet", "-m", "two", cwd=seed)
        git("push", "--quiet", "origin", "HEAD:main", cwd=seed)
        for name in ("clean", "dirty", "parked"):
            git("fetch", "--quiet", "origin", cwd=self.home / name)
        (self.home / "dirty" / "a.txt").write_text("local edit\n")
        git("checkout", "--quiet", "-b", "lane-x", cwd=self.home / "parked")
        script = self.home / "omniclaude" / "scripts" / "converge-canonical-clone.sh"
        script.parent.mkdir(parents=True)
        script.write_text(FAKE_CONVERGE)
        self.clones = {n: self.home / n for n in ("clean", "dirty", "parked")}

    def env(self) -> dict[str, str]:
        # No ONEX_L* variable: an open lane claim on the host must not name these planted clones.
        env = {
            k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "ONEX_L"))
        }
        env.update(OMNI_HOME=str(self.home), GIT_CONFIG_NOSYSTEM="1")
        return env

    def states(self) -> dict[str, dict[str, bytes]]:
        return {name: tree_state(path) for name, path in self.clones.items()}


class InProcessHostSender:
    """Runs the host command the request's argv names against one planted registry."""

    def __init__(self, registry: Registry) -> None:
        self._registry = registry

    def _run(
        self, argv: Sequence[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        if (
            argv[0] == sys.executable
        ):  # the sync engine: it names the clones refused too long
            lines = "".join(
                f"REFUSED_TOO_LONG {path} refused for 400s: parked or dirty\n"
                for path in self._registry.clones.values()
            )
            return subprocess.CompletedProcess(argv, 0, stdout=lines, stderr="")
        return subprocess.run(list(argv), **kwargs)

    def send(self, request: ModelLabWorkUnitRequest) -> ModelLabWorkUnitReceipt:
        env = self._registry.env()
        if "--observe" in request.argv:
            text = "\n".join(observe(self._registry.home, env))
            code = 0
        else:
            handler = HandlerCanonicalCloneDirtyConvergeEffect(
                self._registry.home,
                env=env,
                engine_argv=[sys.executable, "-c", "pass"],
                run=self._run,
            )
            lines = [
                f"{o.status.value.upper():9} {o.clone} refused {o.refused_for_seconds}s: {o.reason}"
                for o in handler.tick()
            ]
            text, code = "\n".join(lines), 0
        return ModelLabWorkUnitReceipt(
            work_unit_id=request.work_unit_id,
            target_host=request.target_host,
            host=request.target_host,
            repo=request.repo,
            commit_sha=request.commit_sha,
            lane=request.lane,
            kind=request.kind,
            status=EnumLabWorkUnitStatus.COMPLETED,
            exit_code=code,
            output_tail=text[-8000:],
        )


async def run_once(registry: Registry, mode: str) -> ModelConvergenceRunCompleted:
    data: dict[str, Any] = yaml.safe_load(
        (NODE_DIR / "overlays" / "in_memory.yaml").read_text()
    )
    data["mode"] = mode
    data["hosts"] = data["hosts"][:1]
    overlay = ModelConvergenceOrchestratorOverlay.model_validate(data)
    handler = HandlerConvergenceScheduled(
        gate=ScheduleGate(NODE_NAME, declaration=overlay),
        sender=InProcessHostSender(registry),
        clock=lambda: T0,
    )
    output = await handler.handle(
        ModelRuntimeTick(
            now=T0,
            tick_id=uuid.uuid4(),
            sequence_number=1,
            scheduled_at=T0,
            correlation_id=uuid.uuid4(),
            scheduler_id="omn24198-test",
            tick_interval_ms=1000,
        )
    )
    run = output.events[0].payload
    assert isinstance(run, ModelConvergenceRunCompleted)
    return run


async def test_observe_leaves_every_clone_byte_identical_and_names_what_act_would_do(
    tmp_path: Path,
) -> None:
    registry = Registry(tmp_path)
    before = registry.states()

    run = await run_once(registry, "observe")

    assert registry.states() == before
    (host,) = run.hosts
    status = {Path(c.clone).name: c.status for c in host.clones}
    assert (
        status
        == {
            "clean": EnumConvergenceCloneStatus.WOULD_MOVE,  # one commit behind its upstream tip
            "dirty": EnumConvergenceCloneStatus.WOULD_REFUSE,
            "parked": EnumConvergenceCloneStatus.WOULD_MOVE,
        }
    )
    assert host.outcome is EnumConvergenceHostOutcome.REFUSED
    assert run.outcome is EnumConvergenceRunOutcome.FAILED


async def test_act_changes_the_parked_clone_and_still_refuses_the_dirty_one_untouched(
    tmp_path: Path,
) -> None:
    registry = Registry(tmp_path)
    before = registry.states()

    run = await run_once(registry, "act")

    after = registry.states()
    assert after["parked"] != before["parked"]
    assert (
        git("symbolic-ref", "--short", "HEAD", cwd=registry.clones["parked"]) == "main"
    )
    assert after["dirty"] == before["dirty"]
    (host,) = run.hosts
    (refusal,) = host.refusals
    assert Path(refusal.clone).name == "dirty"
    assert "modified 1 (a.txt)" in refusal.reason
    assert run.outcome is EnumConvergenceRunOutcome.FAILED


async def test_the_failing_control_a_run_that_acts_while_the_overlay_says_observe_is_caught(
    tmp_path: Path,
) -> None:
    """Positive control for the byte-identical check: running the act form really does change the registry."""
    registry = Registry(tmp_path)
    before = registry.states()

    await run_once(registry, "act")

    assert registry.states() != before


def test_observe_broken_input_a_child_that_is_not_a_readable_clone_is_a_failed_line(
    tmp_path: Path,
) -> None:
    registry = Registry(tmp_path)
    broken = registry.home / "broken"
    (broken / ".git").mkdir(parents=True)  # a .git directory that holds no repository

    lines = observe(registry.home, registry.env())

    (line,) = [text for text in lines if str(broken) in text]
    assert line.startswith("FAILED")
    assert not (broken / ".git" / "index").exists()


def test_observe_reads_no_bare_mirror(tmp_path: Path) -> None:
    registry = Registry(tmp_path)
    (
        registry.home / "mirror.git"
    ).mkdir()  # no .git directory inside: not a canonical clone

    assert not [
        text for text in observe(registry.home, registry.env()) if "mirror" in text
    ]
