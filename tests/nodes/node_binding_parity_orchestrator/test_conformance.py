# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The BUS and STO conformance suites against the passing bindings and each mutation stub."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import pytest
from omnibase_core.models.event_bus.model_event_headers import ModelEventHeaders
from omnibase_core.models.event_bus.model_event_message import ModelEventMessage
from omnibase_core.models.state.model_state_envelope import ModelStateEnvelope

from omnimarket.nodes.node_binding_parity_orchestrator.conformance.suite_bus import (
    BUS_CHECKS,
    run_bus_suite,
)
from omnimarket.nodes.node_binding_parity_orchestrator.conformance.suite_core import (
    fresh,
)
from omnimarket.nodes.node_binding_parity_orchestrator.conformance.suite_sto import (
    STO_CHECKS,
    highest_witness_sequence,
    highest_witness_sequence_as_text,
    run_sto_suite,
)
from omnimarket.nodes.node_binding_parity_orchestrator.conformance.transcript import (
    ModelBusStep,
    ModelBusTranscript,
    ModelStateTranscript,
    RecordingBus,
    TranscriptBus,
    TranscriptMismatchError,
    TranscriptStateStore,
    load_bus_transcript,
    load_state_transcript,
    record_bus_transcript,
    record_state_transcript,
    transcript_state_harness,
)
from omnimarket.nodes.node_binding_parity_orchestrator.protocols.protocol_local_bus import (
    ProtocolLocalBus,
)
from omnimarket.nodes.node_binding_parity_orchestrator.stubs.stub_bus import (
    StubBusDropsLast,
    StubBusDuplicate,
    StubBusOutOfOrder,
    build_reference_bus,
)
from omnimarket.nodes.node_binding_parity_orchestrator.stubs.stub_state import (
    StateHarness,
    build_disk_state_harness,
    build_exists_always_true_harness,
    build_list_keys_ignores_node_filter_harness,
    build_loses_last_write_on_restart_harness,
    build_memory_state_harness,
    build_stale_read_harness,
    build_text_ordered_keys_harness,
)

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).parent / "fixtures"
BUS_NOTE = "Kafka-style bus transcript"
STATE_NOTE = "Postgres-style state transcript"
type PassingStore = Literal["memory", "disk", "text", "transcript"]


async def test_bus_suite_passes_on_the_in_memory_binding() -> None:
    results = await run_bus_suite(fresh(build_reference_bus))
    assert [r.name for r in results] == [name for name, _ in BUS_CHECKS]
    assert all(r.passed for r in results), [r for r in results if not r.passed]


@pytest.mark.parametrize(
    ("factory", "failing_check"),
    [
        (StubBusOutOfOrder, "delivers_in_publish_order"),
        (StubBusDuplicate, "delivers_each_message_once"),
        (StubBusDropsLast, "delivers_every_message"),
    ],
)
async def test_each_bus_mutation_stub_fails_the_suite(
    factory: type[StubBusOutOfOrder | StubBusDuplicate | StubBusDropsLast],
    failing_check: str,
) -> None:
    results = await run_bus_suite(fresh(factory))
    failed = {r.name for r in results if not r.passed}
    assert failing_check in failed


def _state_factory(kind: PassingStore, root: Path) -> Callable[[str], StateHarness]:
    if kind == "disk":
        return lambda name: build_disk_state_harness(root / name)
    if kind == "text":
        return fresh(build_text_ordered_keys_harness)
    if kind == "transcript":
        transcript = load_state_transcript(FIXTURES / "transcript_postgres_state.json")
        return lambda name: transcript_state_harness(transcript, name)
    return fresh(build_memory_state_harness)


@pytest.mark.parametrize("kind", ["memory", "disk", "text", "transcript"])
async def test_sto_suite_passes_on_passing_stores(
    kind: PassingStore, tmp_path: Path
) -> None:
    results = await run_sto_suite(_state_factory(kind, tmp_path))
    assert [r.name for r in results] == [name for name, _ in STO_CHECKS]
    assert all(r.passed for r in results), [r for r in results if not r.passed]


@pytest.mark.parametrize(
    ("factory", "failing_check"),
    [
        (build_loses_last_write_on_restart_harness, "data_survives_restart"),
        (build_stale_read_harness, "overwrite_reads_latest_write"),
        (build_exists_always_true_harness, "exists_tracks_put_and_delete"),
        (
            build_list_keys_ignores_node_filter_harness,
            "list_keys_complete_and_filtered",
        ),
    ],
)
async def test_each_state_mutation_fails_its_named_check(
    factory: Callable[[], StateHarness], failing_check: str
) -> None:
    results = await run_sto_suite(fresh(factory))
    assert failing_check in {r.name for r in results if not r.passed}


@pytest.mark.parametrize("kind", ["memory", "disk", "text", "transcript"])
async def test_text_head_reader_fails_on_every_passing_store(
    kind: PassingStore, tmp_path: Path
) -> None:
    results = await run_sto_suite(
        _state_factory(kind, tmp_path), head_reader=highest_witness_sequence_as_text
    )
    assert {r.name for r in results if not r.passed} == {
        "head_witness_highest_sequence_is_integer"
    }


@pytest.mark.parametrize("kind", ["memory", "disk", "text"])
async def test_integer_head_reader_selects_ten_over_nine(
    kind: PassingStore, tmp_path: Path
) -> None:
    harness = _state_factory(kind, tmp_path)("integer_head")
    for sequence in (10, 9):
        await harness.store.put(
            ModelStateEnvelope(
                node_id="head_witness",
                scope_id=str(sequence),
                data={"sequence": sequence},
                written_at=datetime(2026, 1, 1, tzinfo=UTC),
                contract_fingerprint="test",
            )
        )
    assert await highest_witness_sequence(harness.store, "head_witness") == 10
    assert await highest_witness_sequence_as_text(harness.store, "head_witness") == 9


async def test_bus_suite_passes_on_recorded_transcript() -> None:
    transcript = load_bus_transcript(FIXTURES / "transcript_kafka_bus.json")
    buses: list[TranscriptBus] = []

    def factory(name: str) -> ProtocolLocalBus:
        bus = TranscriptBus(transcript, name)
        buses.append(bus)
        return bus

    results = await run_bus_suite(factory)
    assert all(r.passed for r in results)
    for bus in buses:
        bus.assert_complete()


async def test_state_replay_consumes_entire_sessions_including_restart() -> None:
    transcript = load_state_transcript(FIXTURES / "transcript_postgres_state.json")
    stores: list[TranscriptStateStore] = []

    def factory(name: str) -> StateHarness:
        store = TranscriptStateStore(transcript, name)
        stores.append(store)
        return StateHarness(store, store.restart)

    results = await run_sto_suite(factory)
    assert all(r.passed for r in results)
    for store in stores:
        store.assert_complete()


class _ReordersReplay:
    def __init__(self, inner: ProtocolLocalBus) -> None:
        self._inner = inner
        self._held: tuple[str, bytes | None, bytes, ModelEventHeaders | None] | None = (
            None
        )

    async def start(self) -> None:
        await self._inner.start()

    async def close(self) -> None:
        await self._inner.close()

    async def subscribe(
        self,
        topic: str,
        *,
        on_message: Callable[[ModelEventMessage], Awaitable[None]],
        group_id: str,
    ) -> Callable[[], Awaitable[None]]:
        return await self._inner.subscribe(
            topic, on_message=on_message, group_id=group_id
        )

    async def publish(
        self,
        topic: str,
        key: bytes | None,
        value: bytes,
        headers: ModelEventHeaders | None = None,
    ) -> None:
        if self._held is None:
            self._held = (topic, key, value, headers)
            return
        await self._inner.publish(topic, key, value, headers)
        await self._inner.publish(*self._held)
        self._held = None


async def test_replay_rejects_reordered_publishes() -> None:
    transcript = load_bus_transcript(FIXTURES / "transcript_kafka_bus.json")
    with pytest.raises(TranscriptMismatchError, match="step 2"):
        await run_bus_suite(
            lambda name: _ReordersReplay(TranscriptBus(transcript, name))
        )


async def test_replay_rejects_changed_state_arguments_and_extra_calls() -> None:
    transcript = load_state_transcript(FIXTURES / "transcript_postgres_state.json")
    store = TranscriptStateStore(transcript, "get_missing_returns_none")
    with pytest.raises(TranscriptMismatchError, match="step 0"):
        await store.get("different")
    assert await store.get("missing") is None
    store.assert_complete()
    with pytest.raises(TranscriptMismatchError, match="step 1"):
        await store.get("missing")


async def test_replay_rejects_unconsumed_steps() -> None:
    bus = TranscriptBus(
        load_bus_transcript(FIXTURES / "transcript_kafka_bus.json"),
        "delivers_in_publish_order",
    )
    with pytest.raises(TranscriptMismatchError, match="step 0"):
        bus.assert_complete()
    store = TranscriptStateStore(
        load_state_transcript(FIXTURES / "transcript_postgres_state.json"),
        "data_survives_restart",
    )
    with pytest.raises(TranscriptMismatchError, match="step 0"):
        store.assert_complete()


async def test_bus_recorder_replays_keys_headers_and_multiple_subscribers() -> None:
    steps: list[ModelBusStep] = []
    recorder = RecordingBus(build_reference_bus(), steps)
    delivered: list[bytes] = []

    async def callback(message: ModelEventMessage) -> None:
        delivered.append(message.value)

    headers = ModelEventHeaders(
        source="test", event_type="sample", timestamp=datetime(2026, 1, 1, tzinfo=UTC)
    )
    await recorder.start()
    unsubscribe = await recorder.subscribe(
        "sample", on_message=callback, group_id="one"
    )
    await recorder.subscribe("sample", on_message=callback, group_id="two")
    await recorder.publish("sample", b"key", b"value", headers)
    await unsubscribe()
    await recorder.publish("sample", b"key", b"second", headers)
    await recorder.close()
    assert delivered == [b"value", b"value", b"second"]
    delivered.clear()
    replay = TranscriptBus(
        ModelBusTranscript(source="test", sessions={"sample": steps}), "sample"
    )
    await replay.start()
    unsubscribe = await replay.subscribe("sample", on_message=callback, group_id="one")
    await replay.subscribe("sample", on_message=callback, group_id="two")
    with pytest.raises(TranscriptMismatchError, match="step 3"):
        await replay.publish("sample", b"wrong", b"value", headers)
    await replay.publish("sample", b"key", b"value", headers)
    await unsubscribe()
    await replay.publish("sample", b"key", b"second", headers)
    await replay.close()
    assert delivered == [b"value", b"value", b"second"]


def _fixture_json(transcript: ModelBusTranscript | ModelStateTranscript) -> str:
    return (
        json.dumps(transcript.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
    )


async def test_recordings_match_committed_fixtures() -> None:
    bus = await record_bus_transcript(BUS_NOTE)
    state = await record_state_transcript(STATE_NOTE)
    assert _fixture_json(bus) == (FIXTURES / "transcript_kafka_bus.json").read_text(
        encoding="utf-8"
    )
    assert _fixture_json(state) == (
        FIXTURES / "transcript_postgres_state.json"
    ).read_text(encoding="utf-8")
    assert "not a live capture" in bus.source
    assert "not a live capture" in state.source
