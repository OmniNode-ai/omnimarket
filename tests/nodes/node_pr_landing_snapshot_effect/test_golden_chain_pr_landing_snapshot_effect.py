# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain and error chain of the landing snapshot step (OMN-23409).

Golden: a tick enters, the recorded snapshot ends the chain on the contract's recorded topic.
Error: a watcher that cannot be read, or a bus that refuses the record, ends the chain with the
failed event on the failure topic and no record.
"""

from __future__ import annotations

import importlib
import tomllib
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import get_type_hints
from uuid import uuid4

import pytest
import yaml
from pydantic import BaseModel

from omnimarket.handlers.emission_sink import InMemoryEmissionSink
from omnimarket.nodes.node_pr_landing_snapshot_effect.handlers.handler_pr_landing_snapshot import (
    HandlerPrLandingSnapshot,
)
from omnimarket.nodes.node_pr_landing_snapshot_effect.handlers.snapshot_sources import (
    InMemoryWatcherSource,
    ProtocolWatcherSource,
    watcher_key,
)
from omnimarket.nodes.node_pr_landing_snapshot_effect.models.model_landing_snapshot import (
    EnumSnapshotFailureReason,
    EnumSnapshotStatus,
    ModelLandingSnapshotFailedEvent,
    ModelLandingSnapshotRequest,
    ModelWatcherPrRead,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.handlers.tick_ports import (
    InMemoryTickPublisher,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.models.model_tick_orchestrator import (
    ModelTickSnapshot,
)

pytestmark = pytest.mark.unit

NODE = "node_pr_landing_snapshot_effect"
PACKAGE = f"omnimarket.nodes.{NODE}"
REPO = Path(__file__).resolve().parents[3]
RECORDED_TOPIC = "onex.evt.omnimarket.pr-landing-tick-snapshot-recorded.v1"
FAILED_TOPIC = "onex.evt.omnimarket.pr-landing-snapshot-failed.v1"
COMMAND_TOPIC = "onex.cmd.omnimarket.pr-landing-snapshot-requested.v1"
TICK = (
    REPO / "tests" / "fixtures" / "pr_landing_tick_replay" / "tick_725_omnimarket.json"
)
FAILED_CONCLUSIONS = ("failure", "cancelled", "timed_out")
FLOOR_PIECE = 9


def _tick() -> ModelTickSnapshot:
    return ModelTickSnapshot.model_validate_json(TICK.read_text(encoding="utf-8"))


def _request() -> ModelLandingSnapshotRequest:
    tick = _tick()
    return ModelLandingSnapshotRequest(
        correlation_id=uuid4(), facts=tick.facts, decision=tick.decision
    )


def _watcher() -> InMemoryWatcherSource:
    pr = _tick().facts.prs[0]
    return InMemoryWatcherSource(
        {
            watcher_key(pr.pr): {
                "facts": {"node_id": "PR_golden"},
                "ci": {
                    "sha": pr.head_sha,
                    "red_facts": [["Tests", "failure", "11", "2026-11-10T00:00:00Z"]],
                },
            }
        }
    )


class _UnreadableWatcher:
    def read(self, keys: Sequence[str]) -> dict[str, ModelWatcherPrRead]:
        raise OSError("the watcher state cannot be read")


class _RefusingBus(InMemoryTickPublisher):
    def publish(self, topic: str, event: BaseModel) -> None:
        if topic == RECORDED_TOPIC:
            raise ConnectionError("the broker refused the record")
        super().publish(topic, event)


def _handler(
    *,
    watcher: ProtocolWatcherSource | None = None,
    publisher: InMemoryTickPublisher | None = None,
    emission_sink: InMemoryEmissionSink | None = None,
    floor_piece: int | None = None,
    drill: bool = False,
) -> HandlerPrLandingSnapshot:
    return HandlerPrLandingSnapshot(
        failed_conclusions=FAILED_CONCLUSIONS,
        watcher=watcher or _watcher(),
        publisher=publisher,
        emission_sink=emission_sink,
        floor_piece=floor_piece,
        drill=drill,
    )


def test_node_is_registered_and_its_contract_binds_the_handler() -> None:
    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["project"]["entry-points"]["onex.nodes"][NODE] == PACKAGE
    package = importlib.import_module(PACKAGE)
    assert package.NodePrLandingSnapshotEffect.contract_path.is_file()
    root = Path(package.__file__ or "").parent
    contract = yaml.safe_load((root / "contract.yaml").read_text())
    metadata = yaml.safe_load((root / "metadata.yaml").read_text())
    assert contract["name"] == metadata["name"] == NODE
    assert contract["node_type"] == "EFFECT_GENERIC"
    assert contract["metadata"]["related_tickets"] == ["OMN-23409"]
    binding = contract["handler"]
    handler_class = getattr(
        importlib.import_module(binding["module"]), binding["class"]
    )

    def model(ref: str) -> type[BaseModel]:
        module, _, name = ref.rpartition(".")
        result: object = getattr(importlib.import_module(module), name)
        assert isinstance(result, type)
        assert issubclass(result, BaseModel)
        assert result.model_config == {"frozen": True, "extra": "forbid"}
        return result

    hints = get_type_hints(handler_class.handle)
    assert hints["request"] is model(binding["input_model"])
    assert hints["return"] is model(binding["output_model"])
    assert contract["event_bus"]["subscribe_topics"] == [COMMAND_TOPIC]
    assert contract["event_bus"]["publish_topics"] == [RECORDED_TOPIC, FAILED_TOPIC]
    assert contract["runtime_dispatch"]["terminal_events"] == {
        "success": RECORDED_TOPIC,
        "failure": FAILED_TOPIC,
    }
    assert contract["overlay"]["id"] == NODE


def test_golden_chain_a_tick_ends_in_its_recorded_snapshot() -> None:
    publisher = InMemoryTickPublisher()
    request = _request()
    result = _handler(publisher=publisher).handle(request)
    assert result.status is EnumSnapshotStatus.RECORDED
    assert [t for t, _ in publisher.published] == [RECORDED_TOPIC]
    event = publisher.published[0][1]
    assert isinstance(event, ModelTickSnapshot)
    assert event.facts.tick == request.facts.tick
    assert event.decision == request.decision
    assert event.facts.prs[0].pr_node_id == "PR_golden"
    assert event.facts.prs[0].failed_run_ids == (11,)
    assert result.node_ids_filled == 1
    assert result.prs_with_failed_runs == 1


def test_error_chain_an_unreadable_watcher_ends_in_the_failed_event_and_no_record() -> (
    None
):
    publisher = InMemoryTickPublisher()
    result = _handler(watcher=_UnreadableWatcher(), publisher=publisher).handle(
        _request()
    )
    assert result.status is EnumSnapshotStatus.FAILED
    assert result.failure_reason is EnumSnapshotFailureReason.WATCHER_UNREADABLE
    assert result.snapshot is None
    assert [t for t, _ in publisher.published] == [FAILED_TOPIC]
    event = publisher.published[0][1]
    assert isinstance(event, ModelLandingSnapshotFailedEvent)
    assert event.detail == "OSError"


def test_error_chain_a_refused_record_ends_in_the_failed_event() -> None:
    publisher = _RefusingBus()
    result = _handler(publisher=publisher).handle(_request())
    assert result.failure_reason is EnumSnapshotFailureReason.RECORD_REFUSED
    assert [t for t, _ in publisher.published] == [FAILED_TOPIC]


def test_an_overlay_without_failed_conclusions_is_refused() -> None:
    with pytest.raises(ValueError, match="failed_conclusions"):
        HandlerPrLandingSnapshot(failed_conclusions=())


def _emitting(
    sink: InMemoryEmissionSink,
    *,
    watcher: ProtocolWatcherSource | None = None,
    drill: bool = False,
) -> HandlerPrLandingSnapshot:
    return _handler(
        watcher=watcher, emission_sink=sink, floor_piece=FLOOR_PIECE, drill=drill
    )


def test_golden_floor_chain_is_a_start_then_a_success_with_counters() -> None:
    sink = InMemoryEmissionSink()
    request = _request()
    _emitting(sink).handle(request)
    assert [e.phase for e in sink.events] == ["start", "success"]
    assert {e.correlation_id for e in sink.events} == {request.correlation_id.hex}
    assert {e.node for e in sink.events} == {NODE}
    assert sink.events[1].counters["node_ids_filled"] == 1
    assert sink.events[1].counters["prs"] == len(request.facts.prs)


def test_error_floor_chain_is_a_start_then_a_typed_failure() -> None:
    sink = InMemoryEmissionSink()
    _emitting(sink, watcher=_UnreadableWatcher(), drill=True).handle(_request())
    assert [e.phase for e in sink.events] == ["start", "failure"]
    assert (
        sink.events[1].failure_code
        == EnumSnapshotFailureReason.WATCHER_UNREADABLE.value
    )
    assert all(e.drill for e in sink.events)


def test_the_floor_receipt_fold_counts_the_golden_and_the_error_chain() -> None:
    from omnimarket.models.floor_17a import ModelFloorReceiptInput, ModelPieceDef
    from omnimarket.nodes.node_floor_emission_compute.handlers.handler_floor_receipt import (
        HandlerFloorReceipt,
    )

    sink = InMemoryEmissionSink()
    _emitting(sink).handle(_request())
    _emitting(sink, watcher=_UnreadableWatcher(), drill=True).handle(_request())
    receipts = HandlerFloorReceipt().handle(
        ModelFloorReceiptInput(
            now=datetime.now(UTC) + timedelta(seconds=1),
            emissions=tuple(sink.events),
            pieces=(
                ModelPieceDef(
                    piece=FLOOR_PIECE,
                    name="Landing and merge queue",
                    reading_max_age_s=3600,
                ),
            ),
            joins=(),
            freshness_max_age_s=3600,
            error_max_age_s=3600,
            chain_deadline_s=600,
        )
    )
    piece = next(p for p in receipts.pieces if p.piece == FLOOR_PIECE)
    assert piece.golden_at is not None
    assert piece.error_at is not None
    assert receipts.findings == ()


# --- named mutations: each chain test goes red when its mutation is injected ---


class _SwallowingPublisher(InMemoryTickPublisher):
    """Mutation drop_recorded_event: the publisher takes the record and sends nothing."""

    def publish(self, topic: str, event: BaseModel) -> None:
        return None


class _WrongTopicPublisher(InMemoryTickPublisher):
    """Mutation failure_on_success_topic: a failed tick is announced as recorded."""

    def publish(self, topic: str, event: BaseModel) -> None:
        super().publish(RECORDED_TOPIC, event)


def _golden_chain(publisher: InMemoryTickPublisher) -> None:
    _handler(publisher=publisher).handle(_request())
    assert [t for t, _ in publisher.published] == [RECORDED_TOPIC]


def _error_chain(publisher: InMemoryTickPublisher) -> None:
    _handler(watcher=_UnreadableWatcher(), publisher=publisher).handle(_request())
    assert [t for t, _ in publisher.published] == [FAILED_TOPIC]


NAMED_MUTATIONS = {
    "drop_recorded_event": (_golden_chain, _SwallowingPublisher),
    "failure_on_success_topic": (_error_chain, _WrongTopicPublisher),
}


@pytest.mark.parametrize("name", sorted(NAMED_MUTATIONS))
def test_chain_test_goes_red_under_its_named_mutation(name: str) -> None:
    check, mutated = NAMED_MUTATIONS[name]
    with pytest.raises(AssertionError):
        check(mutated())


def test_chain_tests_are_green_without_a_mutation() -> None:
    _golden_chain(InMemoryTickPublisher())
    _error_chain(InMemoryTickPublisher())
