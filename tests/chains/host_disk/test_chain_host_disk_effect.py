# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chains of the host disk effect (OMN-23867, AC5).

Golden: command -> entries removed -> typed result with the freed bytes. Error: refused outside the root, refused in
use, and a partial failure. Each chain runs on the in-memory bus, where the node reads the command back from the bus
bytes; the golden and the refused-outside-root chains also run on the real dev broker overlay (the lane named by
HOST_DISK_CHAIN_LANE under the workspace root HOST_DISK_CHAIN_ROOT): the command is published to the broker, consumed
from it under a group unique to the run, run by the node, and the result is read back from the broker.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.nodes.node_host_storage_effect.handlers import entry_remover_local
from omnimarket.nodes.node_host_storage_effect.handlers.handler_host_storage import (
    HandlerHostStorage,
)
from omnimarket.nodes.node_host_storage_effect.models.model_host_storage import (
    EnumHostStorageEntryOutcome,
    EnumHostStorageOutcome,
    EnumHostStorageRefusal,
    ModelHostStorageRequest,
    ModelHostStorageResult,
)
from tests.chains.chain_assert import ChainRecorder, assert_chain
from tests.nodes.node_host_storage_effect.disk_inputs import (
    CAPACITY,
    HOST,
    NODE_DIR,
    Dirs,
    holders,
    make_dirs,
    make_entry,
    real_handler,
)

pytestmark = pytest.mark.unit
LANE_ENV, ROOT_ENV = "HOST_DISK_CHAIN_LANE", "HOST_DISK_CHAIN_ROOT"
_CONTRACT = yaml.safe_load((NODE_DIR / "contract.yaml").read_text(encoding="utf-8"))
CMD = _CONTRACT["event_bus"]["subscribe_topics"][0]
EVT = _CONTRACT["event_bus"]["publish_topics"][0]
OUT = EnumHostStorageEntryOutcome


def _command(command: str, key: str, *entries: Path | str) -> ModelHostStorageRequest:
    return ModelHostStorageRequest(
        host=HOST,
        command=command,
        requested_by="chain-test",
        entries=tuple(str(e) for e in entries),
        correlation_id=key,
    )


async def _on_the_in_memory_bus(
    handler: HandlerHostStorage, request: ModelHostStorageRequest, key: str
) -> tuple[ChainRecorder, ModelHostStorageResult]:
    """Publish the command, read it back from the bus bytes, run the node, publish its result."""
    bus = EventBusInmemory()
    recorder = ChainRecorder(bus)
    await recorder.publish(CMD, request, correlation_id=key)
    history = await bus.get_event_history(limit=100)
    carried = next(m for m in history if m.topic == CMD)
    result = await asyncio.to_thread(
        handler.handle,
        ModelHostStorageRequest.model_validate_json(bytes(carried.value)),
    )
    await recorder.publish(EVT, result, correlation_id=key)
    return recorder, result


def _chain_types() -> list[str]:
    return [ModelHostStorageRequest.__name__, ModelHostStorageResult.__name__]


async def test_golden_chain_command_entries_removed_result_with_freed_bytes_in_memory(
    tmp_path: Path,
) -> None:
    key = uuid.uuid4().hex
    dirs = make_dirs(tmp_path)
    first = make_entry(dirs.cache, "pkg-a", size=40_000)
    second = make_entry(dirs.cache, "pkg-b", size=60_000)
    recorder, result = await _on_the_in_memory_bus(
        real_handler(dirs, no_holders=True),
        _command("evict_cache", key, first, second),
        key,
    )
    assert_chain(
        recorder.events,
        expected_event_types=_chain_types(),
        terminal_fields={
            "outcome": EnumHostStorageOutcome.OK,
            "free_bytes_before": CAPACITY - 100_000,
            "free_bytes_after": CAPACITY,
            "correlation_id": key,
        },
        correlation_id=key,
        bus_history_count=await recorder.bus_history_count(),
    )
    assert [r.outcome for r in result.entry_results] == [OUT.REMOVED, OUT.REMOVED]
    assert [r.entry for r in result.entry_results] == [str(first), str(second)]
    assert not first.exists()
    assert not second.exists()


async def test_error_chain_refused_outside_root_in_memory(tmp_path: Path) -> None:
    key = uuid.uuid4().hex
    dirs = make_dirs(tmp_path)
    secret = make_entry(dirs.outside, "secret")
    (dirs.cache / "link-out").symlink_to(dirs.outside)
    recorder, result = await _on_the_in_memory_bus(
        real_handler(dirs, no_holders=True),
        _command("evict_cache", key, secret, dirs.cache / "link-out" / "secret"),
        key,
    )
    assert_chain(
        recorder.events,
        expected_event_types=_chain_types(),
        terminal_fields={
            "outcome": EnumHostStorageOutcome.REFUSED,
            "refusal": EnumHostStorageRefusal.ALL_ENTRIES_REFUSED,
        },
        correlation_id=key,
        bus_history_count=await recorder.bus_history_count(),
    )
    assert [r.outcome for r in result.entry_results] == [OUT.REFUSED_OUTSIDE_ROOT] * 2
    assert (secret / "payload.bin").exists()


async def test_error_chain_refused_in_use_in_memory(tmp_path: Path) -> None:
    key = uuid.uuid4().hex
    dirs = make_dirs(tmp_path)
    held = make_entry(dirs.cache, "pkg-held")
    with holders(held, held / "payload.bin"):
        recorder, result = await _on_the_in_memory_bus(
            real_handler(dirs), _command("evict_cache", key, held), key
        )
    assert_chain(
        recorder.events,
        expected_event_types=_chain_types(),
        terminal_fields={
            "outcome": EnumHostStorageOutcome.REFUSED,
            "refusal": EnumHostStorageRefusal.ALL_ENTRIES_REFUSED,
        },
        correlation_id=key,
        bus_history_count=await recorder.bus_history_count(),
    )
    assert [r.outcome for r in result.entry_results] == [OUT.REFUSED_IN_USE]
    assert held.exists()


async def test_error_chain_partial_failure_in_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = uuid.uuid4().hex
    dirs = make_dirs(tmp_path)
    free = make_entry(dirs.cache, "pkg-free", size=30_000)
    stuck = make_entry(dirs.cache, "pkg-stuck")
    real_remove = entry_remover_local._remove

    def remove(path: Path) -> None:
        # the removal of one entry fails at the filesystem; the other entry's removal is the real one
        if path.name == "pkg-stuck":
            raise PermissionError(13, "Permission denied")
        real_remove(path)

    monkeypatch.setattr(entry_remover_local, "_remove", remove)
    recorder, result = await _on_the_in_memory_bus(
        real_handler(dirs, no_holders=True),
        _command("evict_cache", key, free, stuck),
        key,
    )
    assert_chain(
        recorder.events,
        expected_event_types=_chain_types(),
        terminal_fields={"outcome": EnumHostStorageOutcome.PARTIAL},
        correlation_id=key,
        bus_history_count=await recorder.bus_history_count(),
    )
    assert [r.outcome for r in result.entry_results] == [OUT.REMOVED, OUT.FAILED]
    assert result.entry_results[1].detail == "PermissionError: Permission denied"
    assert result.freed_bytes == 30_000
    assert not free.exists()
    assert stuck.exists()


async def test_the_chain_has_teeth_with_the_result_event_dropped(
    tmp_path: Path,
) -> None:
    """Injected break: the node runs and publishes no result. The chain assertion must see it."""
    key = uuid.uuid4().hex
    dirs = make_dirs(tmp_path)
    entry = make_entry(dirs.cache, "pkg-a")
    recorder = ChainRecorder(EventBusInmemory())
    await recorder.publish(CMD, _command("evict_cache", key, entry), correlation_id=key)
    with pytest.raises(AssertionError):
        assert_chain(
            recorder.events,
            expected_event_types=_chain_types(),
            terminal_fields={"outcome": EnumHostStorageOutcome.OK},
            correlation_id=key,
        )


def _over_the_dev_broker(
    lane: str, root: str, dirs: Dirs, request: ModelHostStorageRequest
) -> dict[str, Any]:
    """Command in over the broker, node runs it, result read back from the broker under a unique group."""
    from omnimarket.handlers.overlay_store.bus_opener import open_lane_bus
    from omnimarket.topic_namespace import apply_topic_namespace

    key = str(request.correlation_id)
    handler = real_handler(dirs, no_holders=True)
    seen: list[dict[str, Any]] = []

    async def run() -> None:
        async def collect(message: Any) -> None:
            payload = json.loads(bytes(message.value))
            if payload.get("correlation_id") == key:
                seen.append(payload)

        async with open_lane_bus(
            bus="kafka", lane=lane, kafka_bootstrap=None, root=Path(root)
        ) as bus:
            await bus.subscribe(
                apply_topic_namespace(EVT),
                on_message=collect,
                group_id=f"host-disk-effect-reader-{key}",
            )

            async def node(message: Any) -> None:
                command = ModelHostStorageRequest.model_validate_json(
                    bytes(message.value)
                )
                if command.correlation_id != key:
                    return
                result = await asyncio.to_thread(handler.handle, command)
                await bus.publish(
                    apply_topic_namespace(EVT),
                    key.encode(),
                    result.model_dump_json().encode(),
                )

            await bus.subscribe(
                apply_topic_namespace(CMD),
                on_message=node,
                group_id=f"host-disk-effect-node-{key}",
            )
            await bus.publish(
                apply_topic_namespace(CMD),
                key.encode(),
                request.model_dump_json().encode(),
            )
            for _ in range(60):
                if seen:
                    break
                await asyncio.sleep(1)

    asyncio.run(run())
    assert len(seen) == 1
    return seen[0]


@pytest.mark.kafka
@pytest.mark.integration
def test_golden_chain_on_the_real_dev_broker(tmp_path: Path) -> None:
    lane, root = os.environ.get(LANE_ENV), os.environ.get(ROOT_ENV)
    if not (lane and root):
        pytest.skip(
            f"Kafka not reachable: {LANE_ENV} and {ROOT_ENV} name no declared bus lane"
        )
    key = uuid.uuid4().hex
    dirs = make_dirs(tmp_path)
    entry = make_entry(dirs.cache, "pkg-a", size=50_000)
    payload = _over_the_dev_broker(
        lane, root, dirs, _command("evict_cache", key, entry)
    )
    result = ModelHostStorageResult.model_validate(payload)
    assert result.outcome is EnumHostStorageOutcome.OK
    assert result.freed_bytes == 50_000
    assert [r.outcome for r in result.entry_results] == [OUT.REMOVED]
    assert not entry.exists()


@pytest.mark.kafka
@pytest.mark.integration
def test_error_chain_refused_outside_root_on_the_real_dev_broker(
    tmp_path: Path,
) -> None:
    lane, root = os.environ.get(LANE_ENV), os.environ.get(ROOT_ENV)
    if not (lane and root):
        pytest.skip(
            f"Kafka not reachable: {LANE_ENV} and {ROOT_ENV} name no declared bus lane"
        )
    key = uuid.uuid4().hex
    dirs = make_dirs(tmp_path)
    secret = make_entry(dirs.outside, "secret")
    payload = _over_the_dev_broker(
        lane, root, dirs, _command("evict_cache", key, secret)
    )
    result = ModelHostStorageResult.model_validate(payload)
    assert result.outcome is EnumHostStorageOutcome.REFUSED
    assert result.refusal is EnumHostStorageRefusal.ALL_ENTRIES_REFUSED
    assert secret.exists()
