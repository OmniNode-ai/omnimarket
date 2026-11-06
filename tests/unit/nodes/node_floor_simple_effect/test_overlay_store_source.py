# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The floor's overlay store row: a stale or storeless read is not ok; a missing record is unread, never blank."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from omnimarket.models.floor_17a import (
    ModelFloorReceiptInput,
    ModelLabSourceDef,
    ModelPieceDef,
)
from omnimarket.nodes.node_floor_emission_compute.handlers.handler_floor_receipt import (
    HandlerFloorReceipt,
)
from omnimarket.nodes.node_floor_simple_effect.handlers.sources import (
    overlay_store_events,
    read_overlay_store,
)

pytestmark = pytest.mark.unit


NOW = datetime(2026, 11, 6, 1, 0, tzinfo=UTC)


class _Overlay:
    sources = (
        ModelLabSourceDef(
            id="overlay_store",
            title="Overlay store",
            kind="overlay_store",
            max_age_s=900,
        ),
    )


def _write(tmp_path: Path, readings: dict[str, dict[str, object]]) -> Path:
    path = tmp_path / "fleet.json"
    path.write_text(json.dumps({"readings": readings}), encoding="utf-8")
    return path


def _reading(state: str, *, store_error: str | None = None) -> dict[str, object]:
    return {
        "state": state,
        "age_s": 10.0,
        "store_error": store_error,
        "detail": "",
        "read_at": "2026-11-06T01:00:00Z",
    }


def test_each_host_is_a_subject_and_only_fresh_with_an_answering_store_is_ok(
    tmp_path: Path,
) -> None:
    read = read_overlay_store(
        _write(
            tmp_path,
            {
                "h1:lab_hosts": _reading("fresh"),
                "h2:lab_hosts": _reading("stale"),
                "h3:lab_hosts": _reading("fresh", store_error="broker refused"),
                "h4:lab_hosts": _reading("unreadable"),
            },
        ),
        _Overlay(),  # type: ignore[arg-type]
        NOW,
    )
    assert read.status == "read"
    assert [(s.name, s.ok) for s in read.subjects] == [
        ("h1:lab_hosts", True),
        ("h2:lab_hosts", False),
        ("h3:lab_hosts", False),
        ("h4:lab_hosts", False),
    ]
    assert read.subjects[2].note == "broker refused"
    assert read.summary == "1 of 4 overlay reads fresh"


def test_a_missing_record_is_unread(tmp_path: Path) -> None:
    read = read_overlay_store(tmp_path / "absent.json", _Overlay(), NOW)  # type: ignore[arg-type]
    assert read.status == "unread"
    assert "cannot be opened" in read.reason


def test_a_record_with_no_readings_is_unread(tmp_path: Path) -> None:
    read = read_overlay_store(_write(tmp_path, {}), _Overlay(), NOW)  # type: ignore[arg-type]
    assert read.status == "unread"


def test_a_fresh_row_is_a_golden_chain(tmp_path: Path) -> None:
    read = read_overlay_store(
        _write(tmp_path, {"h1:lab_hosts": _reading("fresh")}),
        _Overlay(),  # type: ignore[arg-type]
        NOW,
    )
    events = overlay_store_events(read, 17, NOW, "c1")
    assert [e.phase for e in events] == ["start", "success"]
    assert events[1].counters == {"reads_fresh": 1}


def test_a_stale_row_and_a_missing_record_are_typed_error_chains(
    tmp_path: Path,
) -> None:
    stale = read_overlay_store(
        _write(tmp_path, {"h1:lab_hosts": _reading("stale")}),
        _Overlay(),  # type: ignore[arg-type]
        NOW,
    )
    events = overlay_store_events(stale, 17, NOW, "c2")
    assert [e.phase for e in events] == ["start", "failure"]
    assert events[1].failure_code == "overlay_store_not_fresh"
    assert events[1].failure_detail == "h1:lab_hosts"
    unread = read_overlay_store(tmp_path / "absent.json", _Overlay(), NOW)  # type: ignore[arg-type]
    assert overlay_store_events(unread, 17, NOW, "c3")[1].failure_code == (
        "overlay_store_unread"
    )


def test_the_deployed_receipt_fold_counts_both_chains_for_the_piece(
    tmp_path: Path,
) -> None:
    fresh = read_overlay_store(
        _write(tmp_path, {"h1:lab_hosts": _reading("fresh")}),
        _Overlay(),  # type: ignore[arg-type]
        NOW,
    )
    unread = read_overlay_store(tmp_path / "absent.json", _Overlay(), NOW)  # type: ignore[arg-type]
    receipts = HandlerFloorReceipt().handle(
        ModelFloorReceiptInput(
            now=NOW,
            emissions=(
                *overlay_store_events(fresh, 17, NOW, "c4"),
                *overlay_store_events(unread, 17, NOW, "c5"),
            ),
            pieces=(ModelPieceDef(piece=17, name="floor", reading_max_age_s=900),),
            joins=(),
            freshness_max_age_s=900,
            error_max_age_s=86400,
            chain_deadline_s=600,
        )
    )
    assert receipts.pieces[0].golden_at == NOW
    assert receipts.pieces[0].error_at == NOW
    assert receipts.findings == ()


def test_a_host_whose_reader_stopped_reads_not_ok_though_its_last_reading_said_fresh(
    tmp_path: Path,
) -> None:
    old = _reading("fresh")
    old["read_at"] = "2026-11-06T00:30:00Z"
    read = read_overlay_store(
        _write(tmp_path, {"h1:lab_hosts": _reading("fresh"), "h2:lab_hosts": old}),
        _Overlay(),  # type: ignore[arg-type]
        NOW,
    )
    assert [(s.name, s.ok) for s in read.subjects] == [
        ("h1:lab_hosts", True),
        ("h2:lab_hosts", False),
    ]
    assert read.subjects[1].note == "no reading for 1800s, over the 900s bound"
    assert overlay_store_events(read, 17, NOW, "c6")[1].failure_detail == "h2:lab_hosts"
