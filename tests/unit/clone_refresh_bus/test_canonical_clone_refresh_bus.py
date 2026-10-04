# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A merge event refreshes this host's canonical clone, over the in-memory bus (OMN-20496)."""

import asyncio
import json
import threading
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.delegated_test_loop.lab_run_bus import ProtocolBusMessage
from omnimarket.nodes.node_canonical_clone_refresh_effect import (
    CanonicalCloneRefreshHost,
    EnumCanonicalCloneRefreshStatus,
    HandlerCanonicalCloneRefreshEffect,
    ModelCanonicalCloneRefreshReceipt,
    ModelCanonicalCloneRefreshRequest,
    ModelRepoMerged,
    load_canonical_clone_refresh_topics,
    publish_repo_merged,
)

pytestmark = pytest.mark.unit

SHA = "a" * 40


def _merged(
    repo: str = "OmniNode-ai/omnimarket", n: int = 1, **kw: Any
) -> ModelRepoMerged:
    return ModelRepoMerged(
        repo=repo,
        base="dev",
        merge_sha=SHA,
        pr_number=n,
        merged_at=datetime.now(UTC),
        source="test",
        **kw,
    )


class _Handler:
    host_name = "h-test"

    def __init__(self, delay: float = 0.0) -> None:
        self.delay = delay
        self.calls: list[ModelCanonicalCloneRefreshRequest] = []
        self.active = 0
        self.max_active = 0
        self._lock = threading.Lock()

    def handle(
        self, request: ModelCanonicalCloneRefreshRequest
    ) -> ModelCanonicalCloneRefreshReceipt:
        with self._lock:
            self.calls.append(request)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        time.sleep(self.delay)
        with self._lock:
            self.active -= 1
        return ModelCanonicalCloneRefreshReceipt(
            host=self.host_name,
            repo=request.repo,
            status=EnumCanonicalCloneRefreshStatus.ADVANCED,
            target_sha=request.target_sha,
            before_sha="b" * 12,
            after_sha=request.target_sha[:12],
            events_coalesced=request.events,
            duration_ms=1,
            message="",
        )


class _Bus(EventBusInmemory):
    def __init__(self) -> None:
        super().__init__(environment="local", group="clone-refresh-test")
        self.groups: list[tuple[str, str]] = []

    async def subscribe(
        self,
        topic: str,
        on_message: Callable[[Any], Awaitable[None]],
        group_id: str,
    ) -> Callable[[], Awaitable[None]]:
        self.groups.append((topic, group_id))
        return await super().subscribe(topic, on_message=on_message, group_id=group_id)


async def _receipts(bus: _Bus) -> list[dict[str, Any]]:
    got: list[dict[str, Any]] = []

    async def on_receipt(message: ProtocolBusMessage) -> None:
        got.append(json.loads(message.value)["payload"])

    await bus.subscribe(
        load_canonical_clone_refresh_topics().receipt, on_receipt, "probe"
    )
    return got


async def _wait_for(pred: Callable[[], bool], timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


def test_one_merge_runs_one_sync_and_publishes_a_receipt_in_a_per_host_group() -> None:
    async def scenario() -> None:
        bus = _Bus()
        await bus.start()
        handler = _Handler()
        host = CanonicalCloneRefreshHost(bus, handler)
        got = await _receipts(bus)
        await host.start()
        try:
            await publish_repo_merged(bus, _merged())
            await _wait_for(lambda: len(got) == 1)
            assert [c.repo for c in handler.calls] == ["OmniNode-ai/omnimarket"]
            assert handler.calls[0].target_sha == SHA
            assert got[0]["host"] == "h-test"
            assert got[0]["status"] == "advanced"
            topic, group = bus.groups[1]
            assert topic == host.topics.merged
            assert group.endswith(".h-test")
        finally:
            await host.stop()
            await bus.close()

    asyncio.run(scenario())


def test_coalesce_a_burst_of_five_merges_for_one_repo_runs_at_most_two_syncs() -> None:
    async def scenario() -> None:
        bus = _Bus()
        await bus.start()
        handler = _Handler(delay=0.2)
        host = CanonicalCloneRefreshHost(bus, handler)
        got = await _receipts(bus)
        await host.start()
        try:
            for n in range(5):
                await publish_repo_merged(bus, _merged(n=n + 1))
            await _wait_for(lambda: sum(r["events_coalesced"] for r in got) == 5)
            assert 1 <= len(handler.calls) <= 2
            assert handler.max_active == 1
        finally:
            await host.stop()
            await bus.close()

    asyncio.run(scenario())


def test_coalesce_two_repos_sync_independently() -> None:
    async def scenario() -> None:
        bus = _Bus()
        await bus.start()
        handler = _Handler(delay=0.05)
        host = CanonicalCloneRefreshHost(bus, handler)
        got = await _receipts(bus)
        await host.start()
        try:
            await publish_repo_merged(bus, _merged("OmniNode-ai/omnimarket"))
            await publish_repo_merged(bus, _merged("OmniNode-ai/omniclaude"))
            await _wait_for(lambda: len(got) == 2)
            assert sorted(c.repo for c in handler.calls) == [
                "OmniNode-ai/omniclaude",
                "OmniNode-ai/omnimarket",
            ]
        finally:
            await host.stop()
            await bus.close()

    asyncio.run(scenario())


def test_skip_an_event_that_is_not_a_merge_and_an_unreadable_one() -> None:
    async def scenario() -> None:
        bus = _Bus()
        await bus.start()
        handler = _Handler()
        host = CanonicalCloneRefreshHost(bus, handler)
        await host.start()
        try:
            topic = host.topics.merged
            await publish_repo_merged(bus, _merged(state="closed"))
            await bus.publish(topic, None, b"not json")
            await bus.publish(
                topic, None, json.dumps({"payload": {"repo": "x"}}).encode()
            )
            await _wait_for(lambda: host.skipped == 3)
            assert handler.calls == []
        finally:
            await host.stop()
            await bus.close()

    asyncio.run(scenario())


def test_skip_a_repository_outside_the_allowed_owner() -> None:
    with pytest.raises(ValueError, match="repo owner must be OmniNode-ai"):
        _merged(repo="someone-else/omnimarket")


class _Done:
    def __init__(self, stdout: str, rc: int) -> None:
        self.stdout = stdout
        self.stderr = ""
        self.returncode = rc


class _Runner:
    """Stands in for subprocess.run and records each argv it was given."""

    def __init__(self, stdout: str, rc: int = 0) -> None:
        self.stdout = stdout
        self.rc = rc
        self.seen: list[list[str]] = []

    def __call__(self, argv: list[str], **_: Any) -> _Done:
        self.seen.append(argv)
        return _Done(self.stdout, self.rc)


def _runner(stdout: str, rc: int = 0) -> _Runner:
    return _Runner(stdout, rc)


def _request() -> ModelCanonicalCloneRefreshRequest:
    return ModelCanonicalCloneRefreshRequest(
        repo="OmniNode-ai/omnimarket", target_sha=SHA, events=2, sources=["test"]
    )


def test_handler_runs_the_host_sync_command_for_the_repo_and_reads_its_result() -> None:
    run = _runner(f"ADVANCED   /h/omni_home/omnimarket dev 111111111111->{SHA[:12]}\n")
    handler = HandlerCanonicalCloneRefreshEffect(
        ["python3", "sync.py", "sync", "--repo", "{repo}", "--verb", "bus"],
        host_name="h-test",
        run=run,
    )
    receipt = handler.handle(_request())
    assert run.seen == [
        [
            "python3",
            "sync.py",
            "sync",
            "--repo",
            "OmniNode-ai/omnimarket",
            "--verb",
            "bus",
        ]
    ]
    assert receipt.status is EnumCanonicalCloneRefreshStatus.ADVANCED
    assert receipt.before_sha == "111111111111"
    assert receipt.after_sha == SHA[:12]
    assert receipt.events_coalesced == 2


@pytest.mark.parametrize(
    ("stdout", "rc", "status"),
    [
        (f"UP_TO_DATE /h/omnimarket dev {SHA[:12]}->{SHA[:12]}\n", 0, "up_to_date"),
        (
            "REFUSED    /h/omnimarket dev 111111111111-> index.lock present\n",
            0,
            "refused",
        ),
        ("FAILED     /h/omnimarket dev 111111111111-> fetch timed out\n", 1, "failed"),
        ("", 0, "no_clone"),
    ],
)
def test_skip_or_report_each_engine_result(stdout: str, rc: int, status: str) -> None:
    handler = HandlerCanonicalCloneRefreshEffect(
        ["sync", "{repo}"], host_name="h-test", run=_runner(stdout, rc)
    )
    assert handler.handle(_request()).status.value == status


def test_handler_refuses_a_command_without_the_repo_placeholder() -> None:
    with pytest.raises(ValueError, match="must name the repository"):
        HandlerCanonicalCloneRefreshEffect(["sync"], host_name="h")
