# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-22917 review findings on the seven no-file readings: each test fails on the code it names."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.handlers.emission_sink import InMemoryEmissionSink
from omnimarket.nodes.node_reading_writer_effect.protocols import ModelAppendOutcome
from omnimarket.nodes.node_work_ledger_no_file_readings_effect.handlers.handler_no_file_readings import (
    HandlerNoFileReadings,
)
from omnimarket.nodes.node_work_ledger_no_file_readings_effect.handlers.judges import (
    judge_rebuild,
)
from omnimarket.nodes.node_work_ledger_no_file_readings_effect.models import (
    ModelNoFileOverlay,
    ModelNoFileRequest,
    ModelRebuildFacts,
    ModelRowKey,
)
from tests.unit.nodes.node_work_ledger_reader_effect.no_file_world import (
    NOW,
    PACKAGE,
    clean_world,
    overlay,
    world_source,
)

pytestmark = pytest.mark.unit


def test_seven_specs_all_numbered_one_are_refused() -> None:
    document = yaml.safe_load((PACKAGE / "overlay.example.yaml").read_text())
    document["readings"] = [dict(document["readings"][0]) for _ in range(7)]
    with pytest.raises(ValidationError, match="1 to 7 exactly once"):
        ModelNoFileOverlay.model_validate(document)


def test_the_shipped_overlay_numbers_every_reading_once() -> None:
    assert sorted(s.number for s in overlay().readings) == [1, 2, 3, 4, 5, 6, 7]


def key(seq: int, digest: str) -> ModelRowKey:
    return ModelRowKey(seq=seq, digest=digest)


def test_duplicate_seq_in_the_live_table_is_not_collapsed() -> None:
    facts = ModelRebuildFacts(live=(key(1, "a"), key(1, "b")), rebuilt=(key(1, "b"),))
    reasons, judged = judge_rebuild(facts)
    assert judged
    assert reasons == ["live-table-has-duplicate-seq-1-of-1"]


def test_conflicting_duplicates_in_the_rebuilt_table_are_detected() -> None:
    facts = ModelRebuildFacts(live=(key(1, "a"),), rebuilt=(key(1, "a"), key(1, "z")))
    reasons, _ = judge_rebuild(facts)
    assert reasons == ["rebuilt-table-has-duplicate-seq-1-of-1"]


def test_tables_without_duplicates_still_compare_equal() -> None:
    facts = ModelRebuildFacts(
        live=(key(1, "a"), key(2, "b")), rebuilt=(key(2, "b"), key(1, "a"))
    )
    assert judge_rebuild(facts) == ([], True)


class IdRecordingAppender:
    def __init__(self) -> None:
        self.request_ids: list[str] = []

    def append(self, rows: str, request_id: str) -> ModelAppendOutcome:
        self.request_ids.append(request_id)
        return ModelAppendOutcome(status="accepted")


def run_write(correlation_id: str, seconds_later: int) -> list[str]:
    appender = IdRecordingAppender()
    from datetime import timedelta

    node = HandlerNoFileReadings(
        overlay(),
        world_source(clean_world()),
        InMemoryEmissionSink(),
        appender,
        clock=lambda: NOW + timedelta(seconds=seconds_later),
        backend="in_memory",
    )
    node.handle(ModelNoFileRequest(correlation_id=correlation_id, write=True))
    return appender.request_ids


def test_a_redelivery_in_another_second_keeps_every_request_id() -> None:
    assert run_write("run-1", 0) == run_write("run-1", 7)


def test_a_new_correlation_id_is_a_new_probe_with_new_request_ids() -> None:
    first, second = run_write("run-1", 0), run_write("run-2", 0)
    assert len(set(first + second)) == 14


def test_the_example_overlay_stanza_names_each_reading_by_its_own_cell() -> None:
    text = (PACKAGE / "overlay.example.yaml").read_text()
    stanza = [ln for ln in text.splitlines() if ln.startswith("#") and "cells:" in ln]
    assert stanza, "the example shows no source stanza"
    assert all("reading:" in ln for ln in stanza)
    assert (
        Path(PACKAGE / "overlay.example.yaml")
        .read_text()
        .count("one source per reading")
        == 1
    )
