# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chain event tests of the lab_work backend (OMN-24078), on the in-memory bus.

The node's command arrives on the in-memory bus; its handler sends one lab work unit through the real sender over a second
in-memory bus on which a real lab-work host serves (a real git origin, the real local executor); the host runs a script that
prints a result line as the node's command line does, and the typed result leaves on the node's terminal topic.
Shape followed: tests/helpers/lab_test_harness_fakes.py (a real LabWorkHost on the in-memory bus) and the chain file beside this one.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)
from omnibase_infra.event_bus.event_bus_inmemory import (
    EventBusInmemory as InfraEventBusInmemory,
)

from omnimarket.lab_work.bus import LabWorkHost
from omnimarket.nodes.node_gpu_power_caps_effect.handlers.handler_gpu_power_caps import (
    HandlerGpuPowerCaps,
)
from omnimarket.nodes.node_gpu_power_caps_effect.handlers.sender_lab_work_bus import (
    SenderLabWorkBus,
)
from omnimarket.nodes.node_gpu_power_caps_effect.models.model_gpu_power_caps import (
    RESULT_LINE_PREFIX,
    EnumGpuPowerCapsOutcome,
    EnumGpuPowerCapsRefusal,
    ModelGpuPowerCapsRequest,
    ModelGpuPowerCapsResult,
)
from omnimarket.nodes.node_gpu_power_caps_effect.models.model_gpu_power_caps_overlay import (
    ModelGpuPowerCapsOverlay,
)
from omnimarket.nodes.node_lab_work_unit_effect import (
    HandlerHostCapacityAdvertiseEffect,
    HandlerLabWorkUnitEffect,
)
from omnimarket.nodes.node_lab_work_unit_effect.protocols import (
    HostReading,
    LocalShellLabWorkExecutor,
)
from tests.unit.nodes.node_gpu_power_caps_effect.fixtures import NODE_DIR
from tests.unit.nodes.node_gpu_power_caps_effect.test_gpu_power_caps_lab_work import (
    overlay_document,
)

pytestmark = pytest.mark.unit

HOST = "h202"
REPO = "OmniNode-ai/demo"
CONTRACT = yaml.safe_load((NODE_DIR / "contract.yaml").read_text(encoding="utf-8"))
CMD = CONTRACT["event_bus"]["subscribe_topics"][0]
EVT = CONTRACT["event_bus"]["publish_topics"][0]

#: The host script: prints what the node's command line prints, for the host it was asked about.
HOST_SCRIPT = """\
import json, sys
host = sys.argv[1]
mode = sys.argv[3]
if mode == "ok":
    result = {"host": host, "outcome": "ok", "cards": [
        {"name": "Card Big", "wanted_watts": 450, "index": 0, "before_watts": 600,
         "after_watts": 450, "action": "applied"}]}
    print("uv: resolved packages")
    print(PREFIX + json.dumps(result))
elif mode == "silent":
    print("the command ended with no result line")
    sys.exit(5)
"""


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
    (origin / "host_cmd.py").write_text(
        f"PREFIX = {RESULT_LINE_PREFIX!r}\n" + HOST_SCRIPT, encoding="utf-8"
    )
    _git("add", ".", cwd=origin)
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "x", cwd=origin)
    sha = _git("rev-parse", "HEAD", cwd=origin)
    (origin.parent / "demo.git").symlink_to(origin / ".git")
    return f"file://{tmp_path / 'origin'}", sha


def overlay_for(
    sha: str, mode: str, *, command: str | None = None
) -> ModelGpuPowerCapsOverlay:
    doc: dict[str, Any] = overlay_document()
    doc["hosts"] = [
        {"name": HOST, "transport_prefix": [], "caps_unit": "gpu-power-caps-h202"}
    ]
    doc["lab_work"].update(
        repo=REPO,
        commit_sha=sha,
        unit_timeout_seconds=60,
        host_argv=[
            command or sys.executable,
            "host_cmd.py",
            "{host}",
            "{requested_by}",
            mode,
        ],
    )
    return ModelGpuPowerCapsOverlay.model_validate(doc)


def lab_bus_opener(url_base: str, work_root: Path) -> Any:
    """What SenderLabWorkBus opens: an in-memory bus with a real lab-work host serving it. It is built inside the
    sender's own loop, as the real lane bus is."""

    @contextlib.asynccontextmanager
    async def opener(
        lane: str, root: Path | None
    ) -> AsyncIterator[InfraEventBusInmemory]:
        assert lane == "dev"
        bus = InfraEventBusInmemory(environment="local", group="gpu-power-caps-test")
        await bus.start()
        host = LabWorkHost(
            bus,
            HandlerLabWorkUnitEffect(
                HOST,
                LocalShellLabWorkExecutor(work_root, url_base=url_base),
                allowed_executables=frozenset({Path(sys.executable).name}),
            ),
            HandlerHostCapacityAdvertiseEffect(_Reader()),
        )
        await host.start(advertise=False)
        try:
            yield bus
        finally:
            await host.stop()
            await bus.close()

    return opener


async def chain(
    handler: HandlerGpuPowerCaps, request: ModelGpuPowerCapsRequest
) -> list[ModelGpuPowerCapsResult]:
    bus = EventBusInmemory()
    await bus.start()
    seen: list[ModelGpuPowerCapsResult] = []

    async def node(msg: Any) -> None:
        result = await handler.handle(
            ModelGpuPowerCapsRequest.model_validate_json(msg.value)
        )
        await bus.publish(EVT, key=None, value=result.model_dump_json().encode())

    async def floor(msg: Any) -> None:
        seen.append(ModelGpuPowerCapsResult.model_validate_json(msg.value))

    await bus.subscribe(CMD, on_message=node, group_id="gpu-power-caps-node")
    await bus.subscribe(EVT, on_message=floor, group_id="gpu-power-caps-reader")
    await bus.publish(CMD, key=None, value=request.model_dump_json().encode())
    await bus.close()
    return seen


def handler_for(
    overlay: ModelGpuPowerCapsOverlay, url_base: str, tmp_path: Path
) -> HandlerGpuPowerCaps:
    work_root = tmp_path / "work"
    work_root.mkdir()
    lab_work = overlay.lab_work
    assert lab_work is not None
    sender = SenderLabWorkBus(
        lab_work.bus_lane, None, open_bus=lab_bus_opener(url_base, work_root)
    )
    return HandlerGpuPowerCaps(overlay=overlay, sender=sender)


def request() -> ModelGpuPowerCapsRequest:
    return ModelGpuPowerCapsRequest(host=HOST, requested_by="lane-a")


async def test_lab_work_golden_chain_a_command_runs_on_the_host_and_one_result_event_leaves(
    tmp_path: Path,
) -> None:
    url_base, sha = make_origin(tmp_path)
    handler = handler_for(overlay_for(sha, "ok"), url_base, tmp_path)
    seen = await chain(handler, request())
    assert len(seen) == 1
    assert seen[0].outcome is EnumGpuPowerCapsOutcome.OK
    assert seen[0].host == HOST
    assert [(c.name, c.after_watts) for c in seen[0].cards] == [("Card Big", 450)]


async def test_lab_work_error_chain_a_host_command_without_a_result_yields_a_failed_event(
    tmp_path: Path,
) -> None:
    url_base, sha = make_origin(tmp_path)
    handler = handler_for(overlay_for(sha, "silent"), url_base, tmp_path)
    seen = await chain(handler, request())
    assert len(seen) == 1
    assert seen[0].outcome is EnumGpuPowerCapsOutcome.FAILED
    assert seen[0].detail is not None
    assert "exit code 5" in seen[0].detail


async def test_lab_work_error_chain_a_host_that_refuses_the_executable_yields_a_typed_refusal_event(
    tmp_path: Path,
) -> None:
    url_base, sha = make_origin(tmp_path)
    handler = handler_for(
        overlay_for(sha, "ok", command="/bin/not-allowed-here"), url_base, tmp_path
    )
    seen = await chain(handler, request())
    assert len(seen) == 1
    assert seen[0].outcome is EnumGpuPowerCapsOutcome.REFUSED
    assert seen[0].refusal is EnumGpuPowerCapsRefusal.TRANSPORT_UNAVAILABLE


async def test_lab_work_error_chain_reading_has_teeth_when_the_host_names_another_host(
    tmp_path: Path,
) -> None:
    """Injected break: the host answers for a machine that was not asked. The reading must not pass it as the answer."""
    url_base, sha = make_origin(tmp_path)
    overlay = overlay_for(sha, "ok")
    assert overlay.lab_work is not None
    broken = overlay.model_copy(
        update={
            "lab_work": overlay.lab_work.model_copy(
                update={
                    "host_argv": (
                        sys.executable,
                        "host_cmd.py",
                        "another-host",
                        "{requested_by}",
                        "ok",
                        "{host}",
                    )
                }
            )
        }
    )
    seen = await chain(handler_for(broken, url_base, tmp_path), request())
    assert len(seen) == 1
    assert seen[0].outcome is EnumGpuPowerCapsOutcome.FAILED
    assert seen[0].detail is not None
    assert "no readable result" in seen[0].detail
