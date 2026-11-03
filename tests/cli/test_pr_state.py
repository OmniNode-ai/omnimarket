# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``onex pr-state read`` and ``wait`` over the ``pr_state`` projection exposure.

The assertions run twice: against an in-memory bus and projection, and against
Redpanda (the ``kafka``-marked test), so the read surface is shown to work on
both transports. Each side delivers snapshot deltas to a ``SnapshotCache`` that
the real projection API serves from.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import cache
from typing import Any
from uuid import uuid4

import httpx
import pytest
import yaml
from click.testing import CliRunner
from fastapi.testclient import TestClient

from omnimarket.models.pr_state import (
    ModelPrStatePageRequest,
    ModelPrStateProjectionRefusal,
    ModelPrStateReadPolicy,
    ModelPrStateReadUnavailable,
    ModelPrStateWaitRequest,
)
from omnimarket.nodes.node_pr_state_read_effect import policy as policy_module
from omnimarket.nodes.node_pr_state_read_effect.cli import (
    PrStateCliRuntime,
    pr_state_group,
)
from omnimarket.nodes.node_pr_state_read_effect.handlers.handler_pr_state_page_source import (
    UNCONFIGURED,
    HandlerPrStatePageSource,
)
from omnimarket.nodes.node_pr_state_read_effect.handlers.handler_pr_state_wait import (
    HandlerPrStateWait,
)
from omnimarket.nodes.node_pr_state_read_effect.policy import (
    load_pr_state_read_policy,
)
from omnimarket.nodes.node_projection_pr_state.contract_topics import SUBSCRIBE_TOPICS
from omnimarket.projection.api_server import app, get_row_source, get_topic_map
from omnimarket.projection.discovery import build_projection_topic_map
from omnimarket.projection.models import (
    ModelProjectionSnapshotDelta,
    ProjectionTableConfig,
)
from omnimarket.projection.snapshot_cache import SnapshotCache
from tests.helpers.cache_row_source import CacheRowSource

POLICY = load_pr_state_read_policy()
TOPIC = POLICY.projection_topic
SOURCE_TOPIC = SUBSCRIBE_TOPICS[0]
NOW = datetime(2026, 11, 1, 12, 0, 0, tzinfo=UTC)
REPO = "OmniNode-ai/omnimarket"
HEAD = "a" * 40
NEW_HEAD = "b" * 40


def _stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _row(
    repo: str,
    pr_number: int,
    *,
    observed_at: datetime,
    head_sha: str = HEAD,
    verdict: str = "PENDING",
    state: str = "open",
    labels: tuple[str, ...] = ("runtime",),
    draft: bool = False,
) -> dict[str, Any]:
    return {
        "repo": repo,
        "pr_number": pr_number,
        "state": state,
        "head_sha": head_sha,
        "base": "dev",
        "head_ref": f"omn-{pr_number}-change",
        "title": "a change",
        "draft": draft,
        "author": "someone",
        "author_is_bot": False,
        "labels": list(labels),
        "armed": False,
        "queued": False,
        "watcher_class": "WAITING",
        "ci_verdict": verdict,
        "red_contexts": [],
        "pending_contexts": ["lint"] if verdict == "PENDING" else [],
        "ci_read_at": _stamp(observed_at),
        "merged_at": "",
        "observed_at": _stamp(observed_at),
    }


def _delta(row: dict[str, Any], offset: int) -> bytes:
    return (
        ModelProjectionSnapshotDelta(
            topic=TOPIC,
            key=(str(row["repo"]), str(row["pr_number"])),
            op="upsert",
            row=row,
            observed_at=str(row["observed_at"]),
            source_event_id=uuid4().hex,
            source_topic=SOURCE_TOPIC,
            source_partition=0,
            source_offset=offset,
        )
        .model_dump_json()
        .encode()
    )


@cache
def _exposure() -> ProjectionTableConfig:
    return build_projection_topic_map()[TOPIC]


class Clock:
    """A clock the wait loop's sleep advances, with a hook at each sleep."""

    def __init__(self, start: datetime) -> None:
        self.now = start
        self.sleeps: list[float] = []
        self.on_sleep: Callable[[int], None] = lambda _count: None

    def __call__(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += timedelta(seconds=seconds)
        self.on_sleep(len(self.sleeps))


class RecordingClient(httpx.Client):
    """Every request the command makes, so a test can name where they went."""

    def __init__(self, inner: TestClient) -> None:
        super().__init__(base_url="http://projection.test")
        self._inner = inner
        self.paths: list[str] = []
        self.queries: list[dict[str, str]] = []

    def get(self, url: Any, **kwargs: Any) -> Any:
        self.paths.append(str(url))
        self.queries.append(dict(kwargs.get("params") or {}))
        return self._inner.get(url, **kwargs)


@dataclass
class Surface:
    """One transport's projection: deliver a row, run the command."""

    client: RecordingClient
    clock: Callable[[], datetime]
    sleep: Callable[[float], None]
    publish: Callable[[dict[str, Any]], Any]
    repo: str = REPO
    offset: int = field(default=0)
    ticker: Clock | None = None

    def row(self, pr_number: int, **kwargs: Any) -> dict[str, Any]:
        return _row(self.repo, pr_number, **kwargs)

    def invoke(self, *args: str) -> tuple[int, dict[str, Any]]:
        runtime = PrStateCliRuntime(
            client=self.client, clock=self.clock, sleep=self.sleep
        )
        outcome = CliRunner().invoke(pr_state_group, list(args), obj=runtime)
        assert outcome.exception is None or isinstance(outcome.exception, SystemExit)
        return outcome.exit_code, json.loads(outcome.output)


@contextmanager
def _served(cache: SnapshotCache) -> Generator[RecordingClient, None, None]:
    exposure = _exposure()
    app.dependency_overrides[get_row_source] = lambda: CacheRowSource(cache)
    app.dependency_overrides[get_topic_map] = lambda: {TOPIC: exposure}
    try:
        yield RecordingClient(TestClient(app, raise_server_exceptions=True))
    finally:
        app.dependency_overrides.clear()


def _caught_up_cache() -> SnapshotCache:
    cache = SnapshotCache(
        {TOPIC: _exposure()},
        bootstrap_servers="unused:9092",
        group_id=f"test-pr-state-{uuid4().hex[:8]}",
    )
    state = cache._state[TOPIC]
    state.assigned_partitions.add(0)
    state.next_position[0] = 1
    state.end_offsets[0] = 1
    state.bootstrap_complete = True
    state.eof_seen.add(0)
    return cache


@pytest.fixture
def inmemory() -> Generator[Surface, None, None]:
    cache = _caught_up_cache()
    clock = Clock(NOW)
    with _served(cache) as client:
        surface = Surface(
            client=client,
            clock=clock,
            sleep=clock.sleep,
            publish=lambda _row: None,
            ticker=clock,
        )

        def publish(row: dict[str, Any]) -> None:
            surface.offset += 1
            cache.apply_message(TOPIC, None, _delta(row, surface.offset), [])

        surface.publish = publish
        yield surface


def _clock_of(surface: Surface) -> Clock:
    assert surface.ticker is not None
    return surface.ticker


# ---------------------------------------------------------------------------
# The assertions both transports run
# ---------------------------------------------------------------------------


async def assert_read_surface(surface: Surface, *, fresh_age: timedelta) -> None:
    """Fresh data, STALE refusal, absence and the JSON schema, on one transport."""
    fresh_at = surface.clock() - fresh_age
    stale_at = surface.clock() - timedelta(seconds=POLICY.max_row_age_seconds + 30)
    await _deliver(surface, surface.row(11, observed_at=fresh_at, verdict="GREEN"))
    await _deliver(surface, surface.row(12, observed_at=stale_at, verdict="GREEN"))

    code, out = await asyncio.to_thread(surface.invoke, "read", f"{surface.repo}#11")
    (fresh,) = [item["result"] for item in out["results"]]
    assert code == 0
    assert fresh["outcome"] == "FRESH"
    assert set(fresh["row"]) >= {
        "head_sha",
        "labels",
        "draft",
        "required_verdict",
        "observed_at",
    }
    assert fresh["row"]["head_sha"] == HEAD
    assert fresh["row"]["labels"] == ["runtime"]
    assert fresh["row"]["draft"] is False
    assert fresh["row"]["required_verdict"] == "GREEN"
    assert datetime.fromisoformat(fresh["row"]["observed_at"]) == fresh_at

    code, out = await asyncio.to_thread(surface.invoke, "read", f"{surface.repo}#12")
    (stale,) = [item["result"] for item in out["results"]]
    assert code == 3
    assert stale["outcome"] == "STALE"
    assert "row" not in stale
    assert "head_sha" not in stale
    assert stale["row_age_seconds"] > POLICY.max_row_age_seconds
    assert datetime.fromisoformat(stale["observed_at"]) == stale_at

    code, out = await asyncio.to_thread(surface.invoke, "read", f"{surface.repo}#13")
    (absent,) = [item["result"] for item in out["results"]]
    assert code == 4
    assert absent["outcome"] == "ABSENT"


async def _deliver(surface: Surface, row: dict[str, Any]) -> None:
    delivered = surface.publish(row)
    if asyncio.iscoroutine(delivered):
        await delivered


# ---------------------------------------------------------------------------
# In-memory bus and projection
# ---------------------------------------------------------------------------


def test_stale_row_is_refused_not_printed_as_current(inmemory: Surface) -> None:
    observed = NOW - timedelta(seconds=POLICY.max_row_age_seconds + 1)
    inmemory.publish(inmemory.row(21, observed_at=observed, verdict="GREEN"))

    code, out = inmemory.invoke("read", f"{REPO}#21")

    (result,) = [item["result"] for item in out["results"]]
    assert code == 3
    assert result["outcome"] == "STALE"
    assert result["repo"] == REPO
    assert result["pr_number"] == 21
    assert result["row_age_seconds"] == POLICY.max_row_age_seconds + 1
    assert result["max_row_age_seconds"] == POLICY.max_row_age_seconds
    assert datetime.fromisoformat(result["observed_at"]) == observed
    assert "row" not in result
    assert "GREEN" not in json.dumps(out)


def test_stale_boundary_is_the_declared_limit(inmemory: Surface) -> None:
    at_limit = NOW - timedelta(seconds=POLICY.max_row_age_seconds)
    past_limit = NOW - timedelta(seconds=POLICY.max_row_age_seconds, milliseconds=1)
    inmemory.publish(inmemory.row(22, observed_at=at_limit))
    inmemory.publish(inmemory.row(23, observed_at=past_limit))

    _, out = inmemory.invoke("read", f"{REPO}#22", f"{REPO}#23")

    outcomes = [item["result"]["outcome"] for item in out["results"]]
    assert outcomes == ["FRESH", "STALE"]


def test_read_surface_inmemory(inmemory: Surface) -> None:
    asyncio.run(assert_read_surface(inmemory, fresh_age=timedelta(seconds=20)))


def test_read_many_refs_in_one_call(inmemory: Surface) -> None:
    for number in (31, 32, 33):
        inmemory.publish(
            inmemory.row(number, observed_at=NOW - timedelta(seconds=number))
        )

    code, out = inmemory.invoke(
        "read", f"{REPO}#31", f"{REPO}#32", f"{REPO}#33", f"{REPO}#34"
    )

    assert code == 4
    assert [item["result"]["outcome"] for item in out["results"]] == [
        "FRESH",
        "FRESH",
        "FRESH",
        "ABSENT",
    ]
    assert inmemory.client.paths == [f"/projection/{TOPIC}"]


def test_head_filter_ignores_a_row_at_another_head(inmemory: Surface) -> None:
    inmemory.publish(inmemory.row(41, observed_at=NOW, head_sha=HEAD))

    _, at_head = inmemory.invoke("read", f"{REPO}#41", "--head", HEAD)
    _, at_other = inmemory.invoke("read", f"{REPO}#41", "--head", NEW_HEAD)

    assert at_head["results"][0]["result"]["outcome"] == "FRESH"
    assert at_other["results"][0]["result"]["outcome"] == "ABSENT"


def test_unreadable_projection_is_a_typed_result(inmemory: Surface) -> None:
    app.dependency_overrides[get_topic_map] = lambda: {}

    code, out = inmemory.invoke("read", f"{REPO}#51")

    (result,) = [item["result"] for item in out["results"]]
    assert code == 6
    assert result == {
        "outcome": "UNAVAILABLE",
        "status_code": 404,
        "error": "unknown_topic",
    }


def test_wait_returns_when_the_projection_reaches_a_terminal_verdict(
    inmemory: Surface,
) -> None:
    clock = _clock_of(inmemory)
    inmemory.publish(inmemory.row(61, observed_at=NOW, verdict="PENDING"))
    started = clock.now

    def finish(count: int) -> None:
        if count == 2:
            inmemory.publish(inmemory.row(61, observed_at=clock.now, verdict="GREEN"))

    clock.on_sleep = finish

    code, out = inmemory.invoke("wait", f"{REPO}#61", "--timeout", "120")

    (result,) = [item["result"] for item in out["results"]]
    assert code == 0
    assert result["outcome"] == "FRESH"
    assert result["row"]["required_verdict"] == "GREEN"
    elapsed = (clock.now - started).total_seconds()
    assert elapsed == 2 * POLICY.wait_poll_seconds
    assert elapsed < 60
    # the first read, then only rows observed after the last page
    assert inmemory.client.paths == [f"/projection/{TOPIC}"] * 3
    first, second, third = inmemory.client.queries
    assert first == {}
    assert second["since"] == third["since"] == _stamp(NOW)


def test_wait_makes_no_request_beyond_the_projection(inmemory: Surface) -> None:
    clock = _clock_of(inmemory)
    inmemory.publish(inmemory.row(62, observed_at=NOW, verdict="RED"))

    inmemory.invoke("wait", f"{REPO}#62")

    assert inmemory.client.paths
    assert all(path.startswith("/projection/") for path in inmemory.client.paths)
    assert clock.sleeps == []


def test_wait_ignores_a_terminal_row_at_another_head(inmemory: Surface) -> None:
    clock = _clock_of(inmemory)
    inmemory.publish(
        inmemory.row(63, observed_at=NOW, head_sha=NEW_HEAD, verdict="GREEN")
    )

    def push_wanted_head(count: int) -> None:
        if count == 1:
            inmemory.publish(
                inmemory.row(63, observed_at=clock.now, head_sha=HEAD, verdict="RED")
            )

    clock.on_sleep = push_wanted_head

    _, out = inmemory.invoke("wait", f"{REPO}#63", "--head", HEAD, "--timeout", "60")

    (result,) = [item["result"] for item in out["results"]]
    assert result["outcome"] == "FRESH"
    assert result["row"]["head_sha"] == HEAD
    assert result["row"]["required_verdict"] == "RED"


def test_wait_times_out_with_a_typed_result(inmemory: Surface) -> None:
    clock = _clock_of(inmemory)
    inmemory.publish(inmemory.row(64, observed_at=NOW, verdict="PENDING"))

    code, out = inmemory.invoke("wait", f"{REPO}#64", "--timeout", "12")

    (result,) = [item["result"] for item in out["results"]]
    assert code == 5
    assert result["outcome"] == "TIMEOUT"
    assert result["waited_seconds"] == 12
    assert result["timeout_seconds"] == 12
    assert result["last_head_sha"] == HEAD
    assert datetime.fromisoformat(result["last_observed_at"]) == NOW
    assert sum(clock.sleeps) == 12


def test_wait_does_not_return_a_stale_terminal_row(inmemory: Surface) -> None:
    observed = NOW - timedelta(seconds=POLICY.max_row_age_seconds + 60)
    inmemory.publish(inmemory.row(65, observed_at=observed, verdict="GREEN"))

    code, out = inmemory.invoke("wait", f"{REPO}#65", "--timeout", "10")

    (result,) = [item["result"] for item in out["results"]]
    assert code == 5
    assert result["outcome"] == "TIMEOUT"
    assert result["last_head_sha"] is None
    assert datetime.fromisoformat(result["last_observed_at"]) == observed


def test_a_malformed_ref_is_refused_before_any_request(inmemory: Surface) -> None:
    outcome = CliRunner().invoke(
        pr_state_group,
        ["read", "not-a-ref"],
        obj=PrStateCliRuntime(
            client=inmemory.client, clock=inmemory.clock, sleep=inmemory.sleep
        ),
    )

    assert outcome.exit_code == 2
    assert inmemory.client.paths == []


# ---------------------------------------------------------------------------
# The declared values
# ---------------------------------------------------------------------------


def test_declared_topic_is_the_exposure_the_projection_node_declares() -> None:
    contract = yaml.safe_load(
        (
            policy_module.CONTRACT_PATH.parent.parent
            / "node_projection_pr_state"
            / "contract.yaml"
        ).read_text(encoding="utf-8")
    )
    assert contract["projection_api"]["topic"] == TOPIC
    assert "pr_state" in contract["projection_api"]["route_aliases"]
    assert _exposure().bus_backed is True
    assert _exposure().key_columns == ("repo", "pr_number")


def test_the_policy_is_loaded_from_the_contract() -> None:
    assert isinstance(POLICY, ModelPrStateReadPolicy)
    assert POLICY.max_row_age_seconds == 300
    assert POLICY.wait_poll_seconds < 60


def test_every_handler_module_of_the_node_is_bound_by_its_contract() -> None:
    node_dir = policy_module.CONTRACT_PATH.parent
    contract = yaml.safe_load(policy_module.CONTRACT_PATH.read_text(encoding="utf-8"))
    routed = {
        entry["handler"]["module"].rsplit(".", 1)[-1]
        for entry in contract["handler_routing"]["handlers"]
    }
    on_disk = {path.stem for path in (node_dir / "handlers").glob("handler_*.py")}
    assert routed == on_disk
    http = [
        entry["handler"]["name"]
        for entry in contract["handler_routing"]["handlers"]
        if entry["handler"].get("handler_type") == "http"
    ]
    assert http == ["HandlerPrStatePageSource"]


def test_a_page_source_with_no_client_refuses_instead_of_guessing_an_address() -> None:
    answer = HandlerPrStatePageSource().handle(ModelPrStatePageRequest(topic=TOPIC))
    assert answer.result == ModelPrStateProjectionRefusal(
        status_code=0, error=UNCONFIGURED
    )


def test_a_wait_handler_built_with_no_arguments_returns_unavailable() -> None:
    result = HandlerPrStateWait().handle(
        ModelPrStateWaitRequest(
            repo=REPO, pr_number=1, timeout_seconds=5.0, policy=POLICY
        )
    )
    assert result.result == ModelPrStateReadUnavailable(
        status_code=0, error=UNCONFIGURED
    )


# ---------------------------------------------------------------------------
# Redpanda
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.kafka
@pytest.mark.slow
@pytest.mark.asyncio
async def test_read_surface_redpanda(
    integration_kafka_bootstrap: str,
) -> None:
    """The same assertions, with each delta delivered through the Redpanda overlay."""
    from aiokafka import AIOKafkaProducer

    run_id = uuid4().hex[:12]
    repo = f"omn19486-{run_id}/probe"
    cache = SnapshotCache(
        {TOPIC: _exposure()},
        bootstrap_servers=integration_kafka_bootstrap,
        group_id=f"test-pr-state-{run_id}",
    )
    producer = AIOKafkaProducer(bootstrap_servers=integration_kafka_bootstrap)
    offset = 0
    try:
        await cache.start()
        await producer.start()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 60
        while not cache.is_bootstrapped(TOPIC) and loop.time() < deadline:
            await asyncio.sleep(0.5)
        assert cache.is_bootstrapped(TOPIC)

        async def publish(row: dict[str, Any]) -> None:
            nonlocal offset
            offset += 1
            await producer.send_and_wait(
                TOPIC,
                key="|".join((str(row["repo"]), str(row["pr_number"]))).encode(),
                value=_delta(row, offset),
            )
            key = (str(row["repo"]), str(row["pr_number"]))
            deadline = loop.time() + 60
            while loop.time() < deadline:
                cached = cache._state[TOPIC].rows.get(key)
                if (
                    cached is not None
                    and cached.row["observed_at"] == row["observed_at"]
                ):
                    return
                await asyncio.sleep(0.2)
            raise AssertionError(f"{key} never reached the cache from Redpanda")

        with _served(cache) as client:
            surface = Surface(
                client=client,
                clock=lambda: datetime.now(UTC),
                sleep=time.sleep,
                publish=publish,
                repo=repo,
            )
            await assert_read_surface(surface, fresh_age=timedelta(seconds=5))

            # A wait that ends because a verdict arrives over the broker.
            await publish(
                surface.row(71, observed_at=datetime.now(UTC), verdict="PENDING")
            )

            async def finish() -> None:
                await asyncio.sleep(2)
                await publish(
                    surface.row(71, observed_at=datetime.now(UTC), verdict="GREEN")
                )

            finishing = asyncio.create_task(finish())
            began = time.monotonic()
            code, out = await asyncio.to_thread(
                surface.invoke, "wait", f"{repo}#71", "--timeout", "60"
            )
            await finishing
            (result,) = [item["result"] for item in out["results"]]
            assert code == 0
            assert result["outcome"] == "FRESH"
            assert result["row"]["required_verdict"] == "GREEN"
            assert time.monotonic() - began < 60
    finally:
        with suppress(Exception):
            await producer.stop()
        with suppress(Exception):
            await cache.stop()
