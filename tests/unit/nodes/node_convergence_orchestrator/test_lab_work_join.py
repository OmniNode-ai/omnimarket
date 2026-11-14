# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The seam between the convergence orchestrator and the lab-work serve, on the in-memory bus (OMN-24198).

The orchestrator's handler sends one unit per host through the real sender (``SenderLabWorkBus``) over an in-memory bus on which
real lab-work hosts serve (a real git origin, the real local executor). Each host runs the real dirty-converge command in its
``--observe`` form against a planted registry, and the typed result per host is read from what the command printed. A field
renamed on the unit or receipt model, a topic renamed in either contract, or a command whose lines the parser cannot read turns
these red. The real-broker run of the same join is the integrated check on the lab (ticket criterion AC5).
"""

from __future__ import annotations

import contextlib
import datetime as dt
import os
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.handlers.schedule_gate import ScheduleGate
from omnimarket.lab_work.bus import LabWorkHost
from omnimarket.models.model_convergence_orchestrator import (
    EnumConvergenceCloneStatus,
    EnumConvergenceHostOutcome,
    EnumConvergenceRunOutcome,
    ModelConvergenceOrchestratorOverlay,
    ModelConvergenceRunCompleted,
)
from omnimarket.models.model_lab_work_unit import ModelLabWorkUnitReceipt
from omnimarket.nodes.node_convergence_orchestrator.handlers.handler_convergence_scheduled import (
    NODE_NAME,
    HandlerConvergenceScheduled,
)
from omnimarket.nodes.node_gpu_power_caps_effect.handlers.sender_lab_work_bus import (
    SenderLabWorkBus,
)
from omnimarket.nodes.node_lab_work_unit_effect import (
    HandlerHostCapacityAdvertiseEffect,
    HandlerLabWorkUnitEffect,
)
from omnimarket.nodes.node_lab_work_unit_effect.protocols import (
    HostReading,
    LocalShellLabWorkExecutor,
)
from tests.unit.nodes.node_convergence_orchestrator.test_mode_changes_clone import (
    Registry,
)

pytestmark = pytest.mark.unit

NODE_DIR = (
    Path(__file__).resolve().parents[4] / "src" / "omnimarket" / "nodes" / NODE_NAME
)
HOSTS = ("h051", "h053", "h201")
REPO = "OmniNode-ai/demo"
MODULE = "omnimarket.nodes.node_canonical_clone_dirty_converge_effect.handlers.handler_canonical_clone_dirty_converge_effect"
T0 = dt.datetime(2026, 11, 14, 4, 0, tzinfo=dt.UTC)


class _Reader:
    def read(self, tools: list[str]) -> HostReading:
        return HostReading(
            cores=8, load1=0.1, mem_available_bytes=8 * 1024**3, tools=("git",)
        )


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env=scrub_git_location_env(os.environ),
    ).stdout.strip()


def make_origin(tmp_path: Path) -> tuple[str, str]:
    origin = tmp_path / "origin" / "OmniNode-ai" / "demo"
    origin.mkdir(parents=True)
    _git("init", "-q", cwd=origin)
    (origin / "README").write_text("demo\n", encoding="utf-8")
    _git("add", ".", cwd=origin)
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "x", cwd=origin)
    sha = _git("rev-parse", "HEAD", cwd=origin)
    (origin.parent / "demo.git").symlink_to(origin / ".git")
    return f"file://{tmp_path / 'origin'}", sha


def lab_bus_opener(url_base: str, work_root: Path) -> Any:
    @contextlib.asynccontextmanager
    async def opener(
        lane: str | None, root: Path | None
    ) -> AsyncIterator[EventBusInmemory]:
        bus = EventBusInmemory(environment="local", group="convergence-test")
        await bus.start()
        hosts = []
        for name in HOSTS:
            (work_root / name).mkdir(exist_ok=True)
            host = LabWorkHost(
                bus,
                HandlerLabWorkUnitEffect(
                    name,
                    LocalShellLabWorkExecutor(work_root / name, url_base=url_base),
                    allowed_executables=frozenset({Path(sys.executable).name}),
                ),
                HandlerHostCapacityAdvertiseEffect(_Reader()),
            )
            await host.start(advertise=False)
            hosts.append(host)
        try:
            yield bus
        finally:
            for host in hosts:
                await host.stop()
            await bus.close()

    return opener


def overlay_for(
    sha: str, mode: str, registry_root: str, **command: Any
) -> ModelConvergenceOrchestratorOverlay:
    data: dict[str, Any] = yaml.safe_load(
        (NODE_DIR / "overlays" / "in_memory.yaml").read_text()
    )
    data["mode"] = mode
    data["hosts"] = [{"name": h, "registry_root": registry_root} for h in HOSTS]
    data["lab_work"].update(repo=REPO, commit_sha=sha)
    argv = command.get("argv", [sys.executable, "-m", MODULE])
    data["observe_command"] = {"argv": [*argv, "--observe"], "timeout_seconds": 120}
    data["act_command"] = {"argv": list(argv), "timeout_seconds": 120}
    return ModelConvergenceOrchestratorOverlay.model_validate(data)


async def run_chain(
    overlay: ModelConvergenceOrchestratorOverlay, url_base: str, tmp_path: Path
) -> ModelConvergenceRunCompleted:
    work_root = tmp_path / "work"
    work_root.mkdir()
    sender = SenderLabWorkBus(
        overlay.lab_work.bus_lane, None, open_bus=lab_bus_opener(url_base, work_root)
    )
    handler = HandlerConvergenceScheduled(
        gate=ScheduleGate(NODE_NAME, declaration=overlay),
        sender=sender,
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


async def test_the_three_hosts_each_answer_with_their_own_typed_result_from_the_real_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = Registry(tmp_path / "reg")
    monkeypatch.setenv("OMNI_HOME", str(registry.home))
    url_base, sha = make_origin(tmp_path)
    overlay = overlay_for(sha, "observe", str(registry.home))

    run = await run_chain(overlay, url_base, tmp_path)

    assert sorted(h.host for h in run.hosts) == sorted(HOSTS)
    for host in run.hosts:
        assert host.exit_code == 0
        assert host.outcome is EnumConvergenceHostOutcome.REFUSED
        status = {Path(c.clone).name: c.status for c in host.clones}
        assert status["dirty"] is EnumConvergenceCloneStatus.WOULD_REFUSE
        assert status["parked"] is EnumConvergenceCloneStatus.WOULD_MOVE
    assert run.outcome is EnumConvergenceRunOutcome.FAILED


async def test_a_host_command_the_serve_refuses_fails_that_run_and_is_never_read_as_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Injected break on the seam: an executable the hosts do not allow comes back as a refused unit."""
    registry = Registry(tmp_path / "reg")
    monkeypatch.setenv("OMNI_HOME", str(registry.home))
    url_base, sha = make_origin(tmp_path)
    overlay = overlay_for(
        sha, "observe", str(registry.home), argv=["/bin/not-allowed-here"]
    )

    run = await run_chain(overlay, url_base, tmp_path)

    assert {h.outcome for h in run.hosts} == {EnumConvergenceHostOutcome.FAILED}
    assert run.outcome is EnumConvergenceRunOutcome.FAILED


def test_the_receipt_fields_the_dispatcher_reads_exist_on_the_lab_work_receipt() -> (
    None
):
    assert {"status", "exit_code", "output_tail", "detail"} <= set(
        ModelLabWorkUnitReceipt.model_fields
    )
