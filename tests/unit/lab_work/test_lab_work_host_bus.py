# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20105 -- the lab work unit over the in-memory bus, the capacity
advertisement, and the local runner against a real git repository."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

from omnimarket.lab_work.bus import LabWorkCaller, LabWorkHost, load_lab_work_topics
from omnimarket.lab_work.placement import EnumPlacementDecision, EnumRefusalReason
from omnimarket.nodes.node_lab_work_unit_effect import (
    EnumLabWorkUnitStatus,
    HandlerHostCapacityAdvertiseEffect,
    HandlerLabWorkUnitEffect,
    ModelHostCapacityAdvertisement,
    ModelHostCapacityProbeRequest,
    ModelLabWorkUnitReceipt,
    ModelLabWorkUnitRequest,
)
from omnimarket.nodes.node_lab_work_unit_effect.protocols import (
    HostCapacityUnreadableError,
    HostReading,
    LocalShellLabWorkExecutor,
)

pytestmark = pytest.mark.unit

SHA = "4c3985a451847571e2cebd06a6edf1940069946e"


class _Reader:
    def __init__(self, load1: float, cores: int, *, fail: bool = False) -> None:
        self.load1, self.cores, self.fail = load1, cores, fail

    def read(self, tools: list[str]) -> HostReading:
        if self.fail:
            raise HostCapacityUnreadableError("vm_stat has no 'Pages free' line")
        return HostReading(
            cores=self.cores,
            load1=self.load1,
            mem_available_bytes=32 * 1024**3,
            tools=("git", "uv"),
        )


class _Runner:
    def __init__(self) -> None:
        self.ran: list[str] = []

    def run(
        self, request: ModelLabWorkUnitRequest, host_name: str
    ) -> ModelLabWorkUnitReceipt:
        self.ran.append(request.work_unit_id)
        return ModelLabWorkUnitReceipt(
            work_unit_id=request.work_unit_id,
            target_host=request.target_host,
            host=host_name,
            repo=request.repo,
            commit_sha=request.commit_sha,
            status=EnumLabWorkUnitStatus.COMPLETED,
            exit_code=3,
            log_path=f"/lab-run/logs/{request.work_unit_id}.log",
            output_tail="1 failed, 41 passed",
            duration_seconds=1.0,
        )


def _request(host: str, **overrides: object) -> ModelLabWorkUnitRequest:
    fields: dict[str, object] = {
        "work_unit_id": "lw-0123456789abcdef",
        "target_host": host,
        "repo": "OmniNode-ai/omnimarket",
        "commit_sha": SHA,
        "argv": ["uv", "run", "pytest", "tests/unit", "-q"],
        "kind": "test",
        "timeout_seconds": 60,
        "lane": "test-lane",
    }
    fields.update(overrides)
    return ModelLabWorkUnitRequest.model_validate(fields)


def test_host_runs_only_units_addressed_to_it_and_answers_with_the_receipt() -> None:
    async def scenario() -> tuple[ModelLabWorkUnitReceipt, list[str], list[str]]:
        bus = EventBusInmemory(environment="local", group="lab-work-test")
        await bus.start()
        runner_a, runner_b = _Runner(), _Runner()
        host_a = LabWorkHost(
            bus,
            HandlerLabWorkUnitEffect("h202", runner_a),
            HandlerHostCapacityAdvertiseEffect(_Reader(2.0, 32)),
        )
        host_b = LabWorkHost(
            bus,
            HandlerLabWorkUnitEffect("h105", runner_b),
            HandlerHostCapacityAdvertiseEffect(_Reader(2.0, 10)),
        )
        await host_a.start()
        await host_b.start()
        caller = LabWorkCaller(bus, wait_slack_seconds=5)
        await caller.start()
        await host_a.advertise_once()
        await host_b.advertise_once()
        placement = caller.place()
        assert placement.decision is EnumPlacementDecision.PLACED
        receipt = await caller.dispatch(_request(placement.host_name))
        await caller.stop()
        await host_a.stop()
        await host_b.stop()
        await bus.close()
        return receipt, runner_a.ran, runner_b.ran

    receipt, ran_a, ran_b = asyncio.run(scenario())
    assert receipt.host == "h202"
    assert receipt.lane == "test-lane"
    assert receipt.exit_code == 3
    assert receipt.log_path.endswith("lw-0123456789abcdef.log")
    assert "41 passed" in receipt.output_tail
    assert ran_a == ["lw-0123456789abcdef"]
    assert ran_b == []


def test_host_handler_refuses_another_hosts_unit_and_an_unlisted_executable() -> None:
    runner = _Runner()
    handler = HandlerLabWorkUnitEffect("h202", runner)
    wrong = asyncio.run(handler.handle(_request("h105")))
    shell = asyncio.run(
        handler.handle(_request("h202", argv=["bash", "-c", "rm -rf /"]))
    )
    owner = asyncio.run(handler.handle(_request("h202", repo="someone/else")))
    assert {wrong.status, shell.status, owner.status} == {EnumLabWorkUnitStatus.REFUSED}
    assert "executable 'bash'" in shell.detail
    assert runner.ran == []


def test_handler_crash_terminal_preserves_unit_attribution() -> None:
    class CrashingRunner(_Runner):
        def run(
            self, request: ModelLabWorkUnitRequest, host_name: str
        ) -> ModelLabWorkUnitReceipt:
            raise RuntimeError("worker failed")

    async def scenario() -> None:
        bus = EventBusInmemory(environment="local", group="lab-work-crash-test")
        await bus.start()
        host = LabWorkHost(
            bus,
            HandlerLabWorkUnitEffect("h202", CrashingRunner()),
            HandlerHostCapacityAdvertiseEffect(_Reader(0, 32)),
        )
        caller = LabWorkCaller(bus, wait_slack_seconds=1)
        seen: list[dict[str, object]] = []

        async def on_failure(message: object) -> None:
            seen.append(json.loads(message.value))

        await bus.subscribe(
            host.topics.failure, on_message=on_failure, group_id="failure-probe"
        )
        await host.start(advertise=False)
        await caller.start()
        request = _request("h202", kind="lint")
        try:
            receipt = await caller.dispatch(request)
            await host.drain()
            assert receipt.status is EnumLabWorkUnitStatus.INFRA_ERROR
            payload = seen[0]["payload"]
            assert payload["lane"] == request.lane
            assert payload["repo"] == request.repo
            assert payload["commit_sha"] == request.commit_sha
            assert payload["kind"] == request.kind
            assert payload["work_unit_id"] == request.work_unit_id
            assert payload["host"] == "h202"
            assert payload["duration_seconds"] >= 0
        finally:
            await caller.stop()
            await host.stop()
            await bus.close()

    asyncio.run(scenario())


def test_advertise_stamps_its_own_time_and_carries_the_cadence() -> None:
    stamp = datetime(2026, 9, 29, 18, 0, tzinfo=UTC)
    handler = HandlerHostCapacityAdvertiseEffect(_Reader(6.0, 12), now=lambda: stamp)
    ad = handler.handle(
        ModelHostCapacityProbeRequest(
            host_name="h101", cadence_seconds=10, running_units=1
        )
    )
    assert ad.advertised_at == stamp
    assert ad.cadence_seconds == 10
    assert ad.load_per_core == pytest.approx(7.0 / 12)


def test_advertise_unreadable_reading_raises_and_publishes_nothing() -> None:
    with pytest.raises(HostCapacityUnreadableError):
        HandlerHostCapacityAdvertiseEffect(_Reader(0, 0, fail=True)).handle(
            ModelHostCapacityProbeRequest(host_name="h101")
        )

    async def scenario() -> tuple[int, int]:
        bus = EventBusInmemory(environment="local", group="lab-work-test")
        await bus.start()
        topics = load_lab_work_topics()
        seen: list[bytes] = []

        async def on_ad(message: object) -> None:
            seen.append(getattr(message, "value", b""))

        await bus.subscribe(topics.capacity, on_message=on_ad, group_id="probe")
        host = LabWorkHost(
            bus,
            HandlerLabWorkUnitEffect("h101", _Runner()),
            HandlerHostCapacityAdvertiseEffect(_Reader(0, 0, fail=True)),
        )
        published = await host.advertise_once()
        await bus.close()
        return (0 if published is None else 1), len(seen)

    assert asyncio.run(scenario()) == (0, 0)


def test_contract_declares_the_topics_the_bus_uses() -> None:
    topics = load_lab_work_topics()
    assert topics.command == "onex.cmd.omnimarket.lab-work-unit-requested.v1"
    assert topics.success == "onex.evt.omnimarket.lab-work-unit-completed.v1"
    assert topics.capacity == "onex.evt.omnimarket.lab-host-capacity-advertised.v1"
    assert topics.cadence_seconds == 10


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env=scrub_git_location_env(os.environ),
    ).stdout.strip()


def test_local_runner_runs_the_command_at_the_sha_and_keeps_the_log(
    tmp_path: Path,
) -> None:
    origin = tmp_path / "origin" / "OmniNode-ai" / "demo"
    origin.mkdir(parents=True)
    _git("init", "-q", cwd=origin)
    (origin / "probe.py").write_text("import sys\nprint('ran at sha')\nsys.exit(4)\n")
    _git("add", "probe.py", cwd=origin)
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "x", cwd=origin)
    sha = _git("rev-parse", "HEAD", cwd=origin)
    (origin.parent / "demo.git").symlink_to(origin / ".git")
    runner = LocalShellLabWorkExecutor(
        tmp_path / "work", url_base=f"file://{tmp_path / 'origin'}"
    )
    request = _request(
        "h202",
        repo="OmniNode-ai/demo",
        commit_sha=sha,
        argv=[sys.executable, "probe.py"],
    )
    receipt = runner.run(request, "h202")
    assert receipt.status is EnumLabWorkUnitStatus.COMPLETED, receipt.detail
    assert receipt.exit_code == 4
    assert receipt.lane == request.lane
    assert receipt.kind == request.kind
    assert "ran at sha" in receipt.output_tail
    assert Path(receipt.log_path).is_file()
    assert not (tmp_path / "work" / "runs" / request.work_unit_id).exists()


def test_cli_run_exits_69_when_no_pool_host_advertises() -> None:
    from click.testing import CliRunner

    from omnimarket.lab_work import cli

    base = [
        "run",
        "--bus",
        "inmemory",
        "--repo",
        "OmniNode-ai/omnimarket",
        "--sha",
        SHA,
        "--lane",
        "test-lane",
        "--window",
        "0.2",
        "--",
        "uv",
        "run",
        "pytest",
    ]
    result = CliRunner().invoke(cli.lab_work_group, base)
    assert result.exit_code == cli.EXIT_NO_ADVERTISEMENT, result.output
    assert "could_not_check" in result.output


def test_cli_run_exits_75_when_every_host_is_over_the_load_bar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from click.testing import CliRunner

    from omnimarket.lab_work import cli

    runners = [_Runner(), _Runner()]

    @asynccontextmanager
    async def open_busy_pool(**kwargs: object) -> AsyncIterator[EventBusInmemory]:
        bus = EventBusInmemory(environment="local", group="lab-work-busy-test")
        await bus.start()
        hosts = [
            LabWorkHost(
                bus,
                HandlerLabWorkUnitEffect(name, runner),
                HandlerHostCapacityAdvertiseEffect(_Reader(load, cores)),
            )
            for name, runner, load, cores in [
                ("h202", runners[0], 25, 32),
                ("h105", runners[1], 8, 10),
            ]
        ]

        async def collect_busy_hosts(
            caller: LabWorkCaller, window_seconds: float
        ) -> list[ModelHostCapacityAdvertisement]:
            for host in hosts:
                await host.advertise_once()
            return caller.advertisements()

        monkeypatch.setattr(LabWorkCaller, "collect", collect_busy_hosts)
        try:
            yield bus
        finally:
            await bus.close()

    async def unexpected_dispatch(
        caller: LabWorkCaller, request: ModelLabWorkUnitRequest
    ) -> ModelLabWorkUnitReceipt:
        pytest.fail("an over-capacity pool must not receive a work unit")

    monkeypatch.setattr(cli, "open_lab_run_bus", open_busy_pool)
    monkeypatch.setattr(LabWorkCaller, "dispatch", unexpected_dispatch)
    result = CliRunner().invoke(
        cli.lab_work_group,
        [
            "run",
            "--bus",
            "inmemory",
            "--repo",
            "OmniNode-ai/omnimarket",
            "--sha",
            SHA,
            "--lane",
            "test-lane",
            "--window",
            "0.1",
            "--",
            "uv",
            "run",
            "pytest",
        ],
    )
    assert result.exit_code == cli.EXIT_NO_HOST == 75, result.output
    assert "placement: refused (over_capacity)" in result.output
    assert "h202: over the 0.75 load/core bar" in result.output
    assert "h105: over the 0.75 load/core bar" in result.output
    assert "not running it anywhere" in result.output
    assert "lab-work: sent" not in result.output
    assert all(runner.ran == [] for runner in runners)


@pytest.mark.parametrize(
    "malformed",
    [b"\xff", b"{", b"[]", b'{"payload":{"cores":0}}'],
    ids=["invalid-utf8", "invalid-json", "not-an-object", "invalid-capacity"],
)
def test_caller_ignores_malformed_capacity_advertisements(malformed: bytes) -> None:
    async def scenario() -> None:
        stamp = datetime(2026, 9, 29, 18, 0, tzinfo=UTC)
        bus = EventBusInmemory(environment="local", group="lab-work-malformed-test")
        await bus.start()
        caller = LabWorkCaller(bus, now=lambda: stamp)
        await caller.start()
        try:
            await bus.publish(caller.topics.capacity, b"h202", malformed)
            assert caller.advertisements() == []
            assert caller.place().refusal is EnumRefusalReason.COULD_NOT_CHECK
            valid = HandlerHostCapacityAdvertiseEffect(
                _Reader(0, 32), now=lambda: stamp
            ).handle(ModelHostCapacityProbeRequest(host_name="h202"))
            await bus.publish(
                caller.topics.capacity,
                b"h202",
                json.dumps({"payload": valid.model_dump(mode="json")}).encode(),
            )
            assert caller.advertisements() == [valid]
            assert caller.place().host_name == "h202"
        finally:
            await caller.stop()
            await bus.close()

    asyncio.run(scenario())


def test_caller_rejects_an_advertisement_with_zero_total_slots() -> None:
    async def scenario() -> None:
        stamp = datetime(2026, 9, 29, 18, 0, tzinfo=UTC)
        bus = EventBusInmemory(environment="local", group="lab-work-zero-slots-test")
        await bus.start()
        caller = LabWorkCaller(bus, now=lambda: stamp)
        await caller.start()
        try:
            valid = HandlerHostCapacityAdvertiseEffect(
                _Reader(0, 32), now=lambda: stamp
            ).handle(ModelHostCapacityProbeRequest(host_name="h202"))
            payload = valid.model_dump(mode="json")
            payload["max_units"] = 0
            await bus.publish(
                caller.topics.capacity,
                b"h202",
                json.dumps({"payload": payload}).encode(),
            )
            assert caller.advertisements() == []
            placement = caller.place()
            assert placement.decision is EnumPlacementDecision.REFUSED
            assert placement.refusal is EnumRefusalReason.COULD_NOT_CHECK
            assert placement.host_name == ""
        finally:
            await caller.stop()
            await bus.close()

    asyncio.run(scenario())


def test_claude_login_is_a_cached_login_check_not_a_binary_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnimarket.nodes.node_lab_work_unit_effect.protocols import local_host_reader

    calls: list[str] = []
    answers = iter([False, True])

    def fake_login(path: str) -> bool:
        calls.append(path)
        return next(answers)

    monkeypatch.setattr(local_host_reader, "_claude_logged_in", fake_login)
    clock = [0.0]
    reader = local_host_reader.LocalHostReader(now=lambda: clock[0])
    assert "claude-login" not in reader.read(["claude-login"]).tools
    clock[0] += 10
    assert "claude-login" not in reader.read(["claude-login"]).tools
    assert len(calls) == 1, "read once per TTL, not once per beat"
    clock[0] += 301
    assert "claude-login" in reader.read(["claude-login"]).tools
    assert len(calls) == 2
