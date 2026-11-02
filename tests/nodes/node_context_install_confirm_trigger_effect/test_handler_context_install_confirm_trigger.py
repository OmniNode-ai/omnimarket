# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Projection-applied notifications assemble captured compact content (TA.13)."""

from __future__ import annotations

import importlib
import tomllib
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from pydantic import ValidationError

from omnimarket.models.context_install_confirm import EnumInstallFact
from omnimarket.models.context_install_confirm_trigger import (
    EnumInstallConfirmTriggerOutcome,
    ModelInstallConfirmTriggerResult,
    ModelSessionCompactSnapshot,
    ModelSessionContentAppliedEvent,
)
from omnimarket.nodes.node_context_install_confirm_compute.handlers.handler_context_install_confirm import (
    HandlerContextInstallConfirm,
)
from omnimarket.nodes.node_context_install_confirm_trigger_effect.handlers.handler_context_install_confirm_trigger import (
    HandlerContextInstallConfirmTrigger,
)
from omnimarket.nodes.node_projection_compaction_receipts.handlers.handler_projection_compaction_receipts import (
    HandlerProjectionCompactionReceipts,
)
from omnimarket.nodes.node_projection_compaction_receipts.models import (
    ModelCompactionReceiptsProjectionRequest,
)
from tests.nodes.node_context_install_confirm_request_compute.test_handler_context_install_confirm_request import (
    CAPTURED,
    SESSION,
    activation,
    chunks,
    package,
)

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
NODE_NAME = "node_context_install_confirm_trigger_effect"
NODE_DIR = ROOT / "src/omnimarket/nodes" / NODE_NAME


EVENT_ID = "event-1"
APPLIED = ModelSessionContentAppliedEvent(event_id=EVENT_ID, rows_upserted=1)


def snapshot(**kwargs: Any) -> ModelSessionCompactSnapshot:
    return ModelSessionCompactSnapshot(session_id=SESSION, **kwargs)


class FakeSessionCompactSource:
    """An in-memory source that checks the session selected by the trigger."""

    def __init__(self, snapshot: ModelSessionCompactSnapshot | None) -> None:
        self.snapshot = snapshot
        self.events_read: list[str] = []

    def read(self, event_id: str) -> ModelSessionCompactSnapshot | None:
        assert event_id == EVENT_ID
        self.events_read.append(event_id)
        return self.snapshot


def test_custom_compaction_yields_request_with_records_and_activations() -> None:
    reader = FakeSessionCompactSource(
        snapshot(chunks=chunks(package(), 3), activations=(activation(),))
    )
    result = HandlerContextInstallConfirmTrigger().handle(APPLIED, reader=reader)
    assert result.outcome is EnumInstallConfirmTriggerOutcome.REQUEST_READY
    assert result.request is not None
    assert result.request.records
    assert result.request.session_id == SESSION
    assert result.request.activations == reader.snapshot.activations
    assert result.request.newest_captured_at == CAPTURED
    assert result.packages_unreadable == ()
    assert reader.events_read == [EVENT_ID]


def test_no_chunks_yields_no_compact_records_and_no_request() -> None:
    reader = FakeSessionCompactSource(snapshot(activations=(activation(),)))
    result = HandlerContextInstallConfirmTrigger().handle(APPLIED, reader=reader)
    assert result.outcome is EnumInstallConfirmTriggerOutcome.NO_COMPACT_RECORDS
    assert result.request is None
    assert result.packages_unreadable == ()
    assert reader.events_read == [EVENT_ID]


def test_unreadable_only_package_yields_no_request() -> None:
    unreadable = chunks(package())[0].model_copy(update={"content_sha256": "0" * 64})
    reader = FakeSessionCompactSource(
        snapshot(chunks=(unreadable,), activations=(activation(),))
    )
    result = HandlerContextInstallConfirmTrigger().handle(APPLIED, reader=reader)
    assert result.outcome is EnumInstallConfirmTriggerOutcome.ALL_PACKAGES_UNREADABLE
    assert result.request is None
    assert result.packages_unreadable == ("0" * 64,)


def test_same_inputs_twice_yield_equal_results() -> None:
    reader = FakeSessionCompactSource(
        snapshot(chunks=chunks(package(), 3), activations=(activation(),))
    )
    applied = APPLIED
    handler = HandlerContextInstallConfirmTrigger()
    assert handler.handle(applied, reader=reader) == handler.handle(
        applied, reader=reader
    )


def test_readable_and_unreadable_packages_keep_request_and_newest_capture() -> None:
    unreadable = chunks(package())[0].model_copy(
        update={
            "content_sha256": "0" * 64,
            "emitted_at": CAPTURED + timedelta(seconds=10),
        }
    )
    reader = FakeSessionCompactSource(
        snapshot(
            chunks=(*chunks(package(), 3), unreadable), activations=(activation(),)
        )
    )
    result = HandlerContextInstallConfirmTrigger().handle(APPLIED, reader=reader)
    assert result.outcome is EnumInstallConfirmTriggerOutcome.REQUEST_READY
    assert result.request is not None
    assert result.request.records
    assert result.request.newest_captured_at == unreadable.emitted_at
    assert result.packages_unreadable == ("0" * 64,)


def test_readable_empty_package_still_yields_request() -> None:
    reader = FakeSessionCompactSource(snapshot(chunks=chunks("")))
    result = HandlerContextInstallConfirmTrigger().handle(APPLIED, reader=reader)
    assert result.outcome is EnumInstallConfirmTriggerOutcome.REQUEST_READY
    assert result.request is not None
    assert result.request.records == ()
    assert result.packages_unreadable == ()


def test_wrong_session_in_snapshot_is_refused() -> None:
    wrong_session = chunks(package())[0].model_copy(update={"session_id": "other"})
    reader = FakeSessionCompactSource(snapshot(chunks=(wrong_session,)))
    with pytest.raises(ValueError, match="chunk session_id differs"):
        HandlerContextInstallConfirmTrigger().handle(APPLIED, reader=reader)


def test_zero_rows_upserted_reads_nothing_and_publishes_nothing() -> None:
    reader = FakeSessionCompactSource(snapshot(chunks=chunks(package(), 3)))
    result = HandlerContextInstallConfirmTrigger().handle(
        ModelSessionContentAppliedEvent(event_id=EVENT_ID, rows_upserted=0),
        reader=reader,
    )
    assert result.outcome is EnumInstallConfirmTriggerOutcome.NOT_APPLIED
    assert result.confirmation is None
    assert reader.events_read == []


def test_unresolved_session_publishes_nothing() -> None:
    reader = FakeSessionCompactSource(None)
    result = HandlerContextInstallConfirmTrigger().handle(APPLIED, reader=reader)
    assert result.outcome is EnumInstallConfirmTriggerOutcome.SESSION_UNRESOLVED
    assert result.confirmation is None


def test_confirmation_equals_the_compute_node_result_for_the_built_request() -> None:
    reader = FakeSessionCompactSource(
        snapshot(chunks=chunks(package(), 3), activations=(activation(),))
    )
    result = HandlerContextInstallConfirmTrigger().handle(APPLIED, reader=reader)
    assert result.request is not None
    assert result.confirmation == HandlerContextInstallConfirm().handle(result.request)


def test_models_are_frozen_and_forbid_extra_fields() -> None:
    for model in (
        APPLIED,
        snapshot(),
        ModelInstallConfirmTriggerResult(
            outcome=EnumInstallConfirmTriggerOutcome.NO_COMPACT_RECORDS
        ),
    ):
        assert model.model_config["frozen"] is True
        with pytest.raises(ValidationError):
            type(model).model_validate(model.model_dump() | {"unexpected": True})
    with pytest.raises(ValidationError):
        ModelSessionContentAppliedEvent(event_id="", rows_upserted=1)


def test_contract_metadata_handler_and_entry_point() -> None:
    path = NODE_DIR / "contract.yaml"
    contract = yaml.safe_load(path.read_text())
    discovery = discover_contracts_from_paths([path])
    assert not discovery.errors
    assert [node.name for node in discovery.contracts] == [NODE_NAME]
    assert contract["node_type"] == "effect"
    assert contract["descriptor"]["node_archetype"] == "effect"
    handler = contract["handler"]
    assert (
        getattr(importlib.import_module(handler["module"]), handler["class"])
        is HandlerContextInstallConfirmTrigger
    )
    assert contract["input_model"]["name"] == ModelSessionContentAppliedEvent.__name__
    assert contract["output_model"]["name"] == ModelInstallConfirmTriggerResult.__name__
    nodes = NODE_DIR.parent
    producer = yaml.safe_load(
        (nodes / "node_projection_session_content/contract.yaml").read_text()
    )
    consumer = yaml.safe_load(
        (nodes / "node_projection_compaction_receipts/contract.yaml").read_text()
    )
    compute = yaml.safe_load(
        (nodes / "node_context_install_confirm_compute/contract.yaml").read_text()
    )
    applied = producer["terminal_event"]
    requested = compute["terminal_event"]
    assert consumer["event_bus"]["subscribe_topics"] == [requested]
    assert requested == "onex.evt.omnimarket.context-install-confirm-completed.v1"
    assert applied in producer["event_bus"]["publish_topics"]
    assert contract["event_bus"]["subscribe_topics"] == [applied]
    assert contract["runtime_dispatch"]["command_topic"] == applied
    assert contract["event_bus"]["publish_topics"] == [requested]
    assert contract["terminal_event"] == requested
    assert contract["runtime_dispatch"]["terminal_events"]["success"] == requested
    assert contract["metadata"]["ticket"] == "OMN-21115"
    metadata = yaml.safe_load((NODE_DIR / "metadata.yaml").read_text())
    assert metadata["name"] == NODE_NAME
    assert metadata["tickets"] == ["OMN-21115"]
    entries = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"][
        "entry-points"
    ]["onex.nodes"]
    assert entries[NODE_NAME] == f"omnimarket.nodes.{NODE_NAME}"
    assert metadata["entry_points"]["onex.nodes"][NODE_NAME] == entries[NODE_NAME]


def test_trigger_request_feeds_install_confirmation_and_compaction_receipts() -> None:
    reader = FakeSessionCompactSource(
        snapshot(chunks=chunks(package(), 3), activations=(activation(),))
    )
    triggered = HandlerContextInstallConfirmTrigger().handle(APPLIED, reader=reader)
    confirmed = triggered.confirmation
    assert confirmed is not None
    projected = HandlerProjectionCompactionReceipts().handle(
        ModelCompactionReceiptsProjectionRequest(event=confirmed)
    )
    (row,) = projected.rows
    assert row.verdict is EnumInstallFact.INSTALLED
    assert row.activation_id == activation().activation_id


def test_replayed_confirmation_is_written_with_rows_upserted_above_zero() -> None:
    """Rule 7a: a confirmation the trigger publishes reaches the writer as rows, never zero."""
    from tests.test_compaction_receipts_projection_writer import _Db, _writer

    reader = FakeSessionCompactSource(
        snapshot(chunks=chunks(package(), 3), activations=(activation(),))
    )
    handler = HandlerContextInstallConfirmTrigger()
    first = handler.handle(APPLIED, reader=reader).confirmation
    replay = handler.handle(APPLIED, reader=reader).confirmation
    assert first is not None
    assert replay == first
    out = _writer(_Db()).handle(replay.model_dump(mode="json"))
    assert out["rows_upserted"] > 0
    assert {row["verdict"] for row in out["compaction_receipts_rows"]} == {"installed"}


def test_replay_runs_only_the_fold_with_no_process_network_or_transcript_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rule: replay invokes no model, CLI or transcript read, only the fold."""
    import builtins
    import socket
    import subprocess

    def forbidden(*_: object, **__: object) -> None:
        raise AssertionError("replay touched a process, network or file")

    reader = FakeSessionCompactSource(
        snapshot(chunks=chunks(package(), 3), activations=(activation(),))
    )
    confirmation = HandlerContextInstallConfirmTrigger().handle(APPLIED, reader=reader)
    assert confirmation.confirmation is not None
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(builtins, "open", forbidden)
    rows = (
        HandlerProjectionCompactionReceipts()
        .handle(
            ModelCompactionReceiptsProjectionRequest(event=confirmation.confirmation)
        )
        .rows
    )
    assert rows


def test_trigger_chunk_permutations_with_duplicates_yield_one_confirmation() -> None:
    """AC2 through the trigger: chunk order and duplicates never change the result."""
    import random

    ordered = chunks(package(), 3)
    expected = (
        HandlerContextInstallConfirmTrigger()
        .handle(
            APPLIED,
            reader=FakeSessionCompactSource(
                snapshot(chunks=ordered, activations=(activation(),))
            ),
        )
        .confirmation
    )
    assert expected is not None
    rng = random.Random(21115)
    for _ in range(25):
        shuffled = [*ordered, *rng.sample(ordered, k=rng.randint(1, len(ordered)))]
        rng.shuffle(shuffled)
        reader = FakeSessionCompactSource(
            snapshot(chunks=tuple(shuffled), activations=(activation(),))
        )
        got = (
            HandlerContextInstallConfirmTrigger()
            .handle(APPLIED, reader=reader)
            .confirmation
        )
        assert got == expected


def test_trigger_replay_with_a_later_activation_keeps_the_earlier_fact() -> None:
    """AC3 through the trigger: appending a later activation leaves the earlier fact identical."""
    from datetime import UTC, datetime
    from uuid import UUID

    from omnimarket.models.context_install_confirm import ModelPublishedActivation

    handler = HandlerContextInstallConfirmTrigger()
    base = handler.handle(
        APPLIED,
        reader=FakeSessionCompactSource(
            snapshot(chunks=chunks(package(), 3), activations=(activation(),))
        ),
    ).confirmation
    assert base is not None
    later = ModelPublishedActivation(
        activation_id=UUID("9f3c0000-0000-4000-8000-000000000002"),
        installed_messages_hash="f" * 64,
        published_at=datetime(2026, 10, 17, 13, tzinfo=UTC),
    )
    after = handler.handle(
        APPLIED,
        reader=FakeSessionCompactSource(
            snapshot(chunks=chunks(package(), 3), activations=(activation(), later))
        ),
    ).confirmation
    assert after is not None
    earlier = {f.activation_id: f.model_dump_json() for f in base.facts}
    kept = {
        f.activation_id: f.model_dump_json()
        for f in after.facts
        if f.activation_id in earlier
    }
    assert kept == earlier
