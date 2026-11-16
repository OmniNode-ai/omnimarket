# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""replay_from_file: rows the ledger file holds and the projection never received are re-emitted
by row id through the work-ledger emit path (OMN-19998).

Golden, error and control cases, plus the seams: the parity report's missing-row key selects the
row, the emitted event validates as the projection's typed event, and the replay source is one the
projection accepts without a ledger_seq.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from pydantic import TypeAdapter, ValidationError

from omnimarket.events.model_ledger_row_event import (
    ModelLedgerRowEvent,
    work_ledger_event_id,
    work_ledger_row_id,
)
from omnimarket.nodes.node_event_emit_effect.handlers.handler_event_emit_effect import (
    HandlerEventEmitEffect,
)
from omnimarket.nodes.node_event_emit_effect.models.model_emit_request import (
    ModelEmitRequest,
)
from omnimarket.nodes.node_event_emit_effect.models.model_emit_result import (
    ModelEmitResult,
)
from omnimarket.nodes.node_event_emit_effect.spool.spool_outbox import SpoolOutbox
from omnimarket.nodes.node_projection_work_ledger.parity import compare
from omnimarket.nodes.node_work_ledger_emit_effect.handlers.handler_work_ledger_emit import (
    HandlerWorkLedgerEmit,
    ProtocolEventEmitter,
)
from omnimarket.nodes.node_work_ledger_file_mode_replay_effect.handlers.emit_path_publisher import (
    EmitPathPublisher,
)
from omnimarket.nodes.node_work_ledger_file_mode_replay_effect.handlers.handler_file_mode_replay import (
    HandlerFileModeReplay,
)
from omnimarket.nodes.node_work_ledger_file_mode_replay_effect.handlers.ledger_file_rows import (
    FileLedgerFileRows,
    InMemoryLedgerFileRows,
    failure_log_row_ids,
)
from omnimarket.nodes.node_work_ledger_file_mode_replay_effect.handlers.replay_queue import (
    InMemoryReplayQueue,
    JsonlReplayQueue,
)
from omnimarket.nodes.node_work_ledger_file_mode_replay_effect.handlers.replay_wiring import (
    build_replay_handler,
)
from omnimarket.nodes.node_work_ledger_file_mode_replay_effect.models.model_file_mode_replay import (
    EnumReplayOperation,
    ModelFileModeReplayRequest,
    ModelFileModeReplayResult,
    ModelReplayOverlay,
)

pytestmark = pytest.mark.unit

NODE_ROOT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_work_ledger_file_mode_replay_effect"
)
SOURCE = "ledger-emit"
ROWS = [
    "2026-11-10T16:17:01Z | CLAIM | lane=alpha-1 | ticket=OMN-1 | actor=claude:sonnet5:subagent | repo=omnimarket | est ~1 lane-hours; displaces nothing; (OMN-1) | do the thing",
    "2026-11-10T16:18:02Z | STATUS | lane=alpha-1 | ticket=OMN-1 | repo=omnimarket | state=green | a status line",
    "2026-11-10T16:19:03Z | TERMINAL | lane=alpha-1 | ticket=OMN-1 | outcome=handed-off | friction=none | delegated=0 delegation_reason=none needed",
    "2026-11-10T17:30:00Z | MSG | from=alpha-1 | to=beta-2 | id=2026-11-10T17:30:00Z-alpha-1 | outside the window",
]
IDS = [work_ledger_row_id(r) for r in ROWS]


class _Spool:
    """The emit-effect spool: published only while the bus is up; records every request."""

    def __init__(self, *, up: bool = True, raises: Exception | None = None) -> None:
        self.up = up
        self.raises = raises
        self.requests: list[ModelEmitRequest] = []

    def handle(self, request: ModelEmitRequest) -> ModelEmitResult:
        if self.raises is not None:
            raise self.raises
        self.requests.append(request)
        return ModelEmitResult(event_id=request.event_id, published=self.up)


def _overlay(tmp_path: Path, **over: object) -> ModelReplayOverlay:
    fields: dict[str, object] = {
        "backend": "file",
        "queue_path": str(tmp_path / "queue.jsonl"),
        "ledger_path": str(tmp_path / "ledger.md"),
        "archive_dir": str(tmp_path / "archive"),
        "archive_glob": "*-split.md",
        "replay_source": SOURCE,
    }
    fields.update(over)
    return ModelReplayOverlay.model_validate(fields)


def _handler(
    emitter: ProtocolEventEmitter,
    queue: InMemoryReplayQueue | None = None,
    rows: list[str] | None = None,
) -> HandlerFileModeReplay:
    return HandlerFileModeReplay(
        queue=queue if queue is not None else InMemoryReplayQueue(),
        publisher=EmitPathPublisher(
            emit=HandlerWorkLedgerEmit(emitter=emitter), source=SOURCE
        ),
        file_rows=InMemoryLedgerFileRows(ROWS if rows is None else rows),
    )


def _replay(
    handler: HandlerFileModeReplay,
    *,
    row_ids: list[str] | None = None,
    window_since: datetime | None = None,
    window_until: datetime | None = None,
) -> ModelFileModeReplayResult:
    return handler.handle(
        ModelFileModeReplayRequest(
            operation=EnumReplayOperation.REPLAY_FROM_FILE,
            row_ids=row_ids or [],
            window_since=window_since,
            window_until=window_until,
        )
    )


@pytest.fixture(autouse=True)
def _spool_only_lane(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ONEX_EMIT_EFFECT_SPOOL_ONLY", "true")
    monkeypatch.delenv("KAFKA_BOOTSTRAP_SERVERS", raising=False)


# --- golden -----------------------------------------------------------------------------------


def test_the_named_rows_are_re_emitted_from_the_file_under_the_replay_source() -> None:
    spool = _Spool()
    result = _replay(_handler(spool), row_ids=[IDS[0], IDS[2]])
    assert result.replayed == [IDS[0], IDS[2]]
    assert result.selected == 2
    assert result.still_pending == []
    assert result.refused == []
    assert result.not_in_file == []
    assert [r.correlation_id for r in spool.requests] == [IDS[0], IDS[2]]
    for request, row in zip(spool.requests, [ROWS[0], ROWS[2]], strict=True):
        payload = request.payload
        assert isinstance(payload, dict)
        assert payload["raw_row"] == row
        assert payload["source"] == SOURCE
        assert "ledger_seq" not in payload
        assert request.event_id == str(
            work_ledger_event_id(str(payload["ledger_id"]), str(payload["row_id"]))
        )


def test_a_window_re_emits_exactly_the_rows_stamped_inside_it() -> None:
    spool = _Spool()
    result = _replay(
        _handler(spool),
        window_since=datetime(2026, 11, 10, 16, 15, 0, tzinfo=UTC),
        window_until=datetime(2026, 11, 10, 16, 20, 5, tzinfo=UTC),
    )
    assert result.replayed == IDS[:3]
    assert IDS[3] not in {r.correlation_id for r in spool.requests}


# --- control: a replay of rows already present changes nothing --------------------------------


def test_a_second_replay_of_rows_already_replayed_sends_nothing() -> None:
    spool = _Spool()
    handler = _handler(spool)
    first = _replay(handler, row_ids=IDS[:3])
    sent = len(spool.requests)
    second = _replay(handler, row_ids=IDS[:3])
    assert first.replayed == IDS[:3]
    assert second.replayed == []
    assert second.already_replayed == IDS[:3]
    assert len(spool.requests) == sent == 3


def test_a_replay_with_a_lost_queue_carries_the_same_event_ids(tmp_path: Path) -> None:
    """Idempotent by row id even when no queue remembers the first replay: each row's event id is
    its row id's UUID5, so a second send is the same event and the projection folds it on row_id.
    (The spool keeps one file per send; the identity is the event id, not the file.)"""
    spool_dir = tmp_path / "spool"
    for _ in range(2):
        handler = _handler(HandlerEventEmitEffect(spool=SpoolOutbox(spool_dir)))
        _replay(handler, row_ids=IDS[:3])
    payloads = [
        json.loads(f.read_text())["payload"] for f in sorted(spool_dir.glob("*.json"))
    ]
    assert len(payloads) == 6
    assert {p["row_id"] for p in payloads} == set(IDS[:3])
    assert {p["event_id"] for p in payloads} == {
        str(work_ledger_event_id("rolling-work-ledger", row_id)) for row_id in IDS[:3]
    }
    event: ModelLedgerRowEvent = TypeAdapter(ModelLedgerRowEvent).validate_python(
        payloads[0]
    )
    assert event.source == SOURCE
    assert event.row_id in IDS


# --- error chain ------------------------------------------------------------------------------


def test_an_id_the_file_does_not_hold_is_named_and_nothing_is_sent_for_it() -> None:
    spool = _Spool()
    unknown = "0" * 64
    result = _replay(_handler(spool), row_ids=[IDS[1], unknown])
    assert result.not_in_file == [unknown]
    assert result.replayed == [IDS[1]]
    assert [r.correlation_id for r in spool.requests] == [IDS[1]]


def test_a_bus_that_only_spools_leaves_a_retry_record_until_it_publishes() -> None:
    spool = _Spool(up=False)
    queue = InMemoryReplayQueue()
    handler = _handler(spool, queue)
    held = _replay(handler, row_ids=[IDS[0]])
    assert held.replayed == []
    assert [(p.row_id, p.attempts) for p in held.still_pending] == [(IDS[0], 1)]
    assert queue.pending_row_ids() == {IDS[0]}
    spool.up = True
    healed = _replay(handler, row_ids=[IDS[0]])
    assert healed.replayed == [IDS[0]]
    assert queue.pending_row_ids() == set()


def test_a_spool_that_raises_is_a_retry_not_a_loss() -> None:
    queue = InMemoryReplayQueue()
    result = _replay(
        _handler(_Spool(raises=RuntimeError("spool full")), queue), row_ids=[IDS[0]]
    )
    assert result.replayed == []
    assert result.still_pending[0].row_id == IDS[0]
    assert "spool full" in result.still_pending[0].last_reason
    assert queue.pending_row_ids() == {IDS[0]}


def test_a_row_that_cannot_be_typed_is_refused_with_its_reason() -> None:
    spool = _Spool()
    bad = "2026-11-10T16:17:01Z | NOTE | lane=x | not a canonical type"
    handler = _handler(spool, rows=[bad])
    result = _replay(handler, row_ids=[work_ledger_row_id(bad)])
    assert [r.row_id for r in result.refused] == [work_ledger_row_id(bad)]
    assert spool.requests == []


def test_a_durable_queue_keeps_the_retry_record_across_handlers(tmp_path: Path) -> None:
    overlay = _overlay(tmp_path)
    (tmp_path / "ledger.md").write_text("\n".join(ROWS) + "\n")
    first = build_replay_handler(overlay, emitter=_Spool(up=False))
    held = _replay(first, row_ids=[IDS[0]])
    assert held.still_pending[0].attempts == 1
    assert JsonlReplayQueue(tmp_path / "queue.jsonl").pending_row_ids() == {IDS[0]}
    second = build_replay_handler(overlay, emitter=_Spool(up=True))
    done = _replay(second, row_ids=[IDS[0]])
    assert done.replayed == [IDS[0]]


@pytest.mark.parametrize(
    "selection",
    [
        {},
        {
            "row_ids": ["a" * 64],
            "window_since": datetime(2026, 1, 1, tzinfo=UTC),
            "window_until": datetime(2026, 1, 2, tzinfo=UTC),
        },
        {"window_since": datetime(2026, 1, 1, tzinfo=UTC)},
    ],
)
def test_a_selection_that_is_not_exact_is_refused(selection: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ModelFileModeReplayRequest(
            operation=EnumReplayOperation.REPLAY_FROM_FILE, **selection
        )


def test_row_ids_on_another_operation_are_refused() -> None:
    with pytest.raises(ValidationError):
        ModelFileModeReplayRequest(operation=EnumReplayOperation.DRAIN, row_ids=["a"])


def test_the_overlay_must_name_the_ledger_and_the_replay_source(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="ledger_path"):
        build_replay_handler(
            ModelReplayOverlay(backend="inmemory", replay_source=SOURCE)
        )
    with pytest.raises(ValueError, match="replay_source"):
        build_replay_handler(
            ModelReplayOverlay(backend="inmemory", ledger_path=str(tmp_path))
        )


def test_the_file_reader_reads_the_archive_splits_then_the_live_file(
    tmp_path: Path,
) -> None:
    (tmp_path / "archive").mkdir()
    (tmp_path / "archive" / "ledger-2026-11-10-split.md").write_text(
        ROWS[0] + "\n" + ROWS[1] + "\n"
    )
    (tmp_path / "ledger.md").write_text(ROWS[2] + "\n")
    reader = FileLedgerFileRows(
        tmp_path / "ledger.md", tmp_path / "archive", "*-split.md"
    )
    assert list(reader.rows()) == ROWS[:3]


# --- seams ------------------------------------------------------------------------------------


def test_seam_the_parity_reports_missing_row_key_selects_the_row_in_the_file() -> None:
    """Parity side: the key of a row_missing_in_projection mismatch is the row id this verb takes."""
    report = compare(
        file_rows=ROWS,
        projection_rows={IDS[3]: ROWS[3][:20]},
        projection_state={},
        since=datetime(2026, 11, 10, tzinfo=UTC),
        until=datetime(2026, 11, 10, 23, 59, 59, tzinfo=UTC),
        projection_raw_rows={IDS[3]: ROWS[3]},
    )
    missing = [
        m.key for m in report.mismatches if m.kind.value == "row_missing_in_projection"
    ]
    assert set(missing) == set(IDS[:3])
    result = _replay(_handler(_Spool()), row_ids=missing)
    assert set(result.replayed) == set(missing)
    assert result.not_in_file == []


def test_seam_the_projection_accepts_the_replay_source_without_a_ledger_seq() -> None:
    """Projection side: a source it lists in sequence_required_sources would be sent to the
    poison topic for want of a ledger_seq, so the overlay's replay source must not be one."""
    overlay = yaml.safe_load((NODE_ROOT / "overlay.example.yaml").read_text())
    projection = yaml.safe_load(
        (NODE_ROOT.parent / "node_projection_work_ledger" / "contract.yaml").read_text()
    )
    required = json.dumps(projection).count("sequence_required_sources")
    assert required >= 1
    listed = _find_key(projection, "sequence_required_sources")
    assert overlay["replay_source"] not in listed


def _find_key(node: object, key: str) -> list[str]:
    if isinstance(node, dict):
        for name, value in node.items():
            if name == key:
                return list(value)
            found = _find_key(value, key)
            if found:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _find_key(item, key)
            if found:
                return found
    return []


def test_the_dual_writes_failure_log_becomes_retry_records(tmp_path: Path) -> None:
    """Every row id a failed emit left in the failure log is replayed when the file holds it; a copy's
    row (not in the ledger of record) is named, never sent; a torn line is skipped."""
    copy_row_id = "f" * 64
    log = tmp_path / "work-ledger-emit-failures.jsonl"
    log.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "ts": "2026-11-10T16:17:03Z",
                        "reason": "appender timed out",
                        "row_id": IDS[0],
                    }
                ),
                "{not json",
                json.dumps(
                    {
                        "ts": "2026-11-10T16:18:03Z",
                        "reason": "appender timed out",
                        "row_id": IDS[1],
                    }
                ),
                json.dumps(
                    {
                        "ts": "2026-11-10T16:18:04Z",
                        "reason": "a copy emits nothing",
                        "row_id": copy_row_id,
                    }
                ),
                json.dumps(
                    {"ts": "2026-11-10T16:18:05Z", "reason": "again", "row_id": IDS[0]}
                ),
            ]
        )
        + "\n"
    )
    ids = failure_log_row_ids(log)
    assert ids == [IDS[0], IDS[1], copy_row_id]
    spool = _Spool()
    result = _replay(_handler(spool), row_ids=ids)
    assert result.replayed == [IDS[0], IDS[1]]
    assert result.not_in_file == [copy_row_id]


def _overlay_root(tmp_path: Path, document: dict[str, object]) -> Path:
    root = tmp_path / "overlays"
    target = root / "node_work_ledger_file_mode_replay_effect"
    target.mkdir(parents=True)
    (target / "overlay.yaml").write_text(yaml.safe_dump(document))
    return root


def test_the_command_form_reads_its_settings_from_the_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from click.testing import CliRunner

    from omnimarket.nodes.node_work_ledger_file_mode_replay_effect.__main__ import (
        EXIT_OVERLAY_INVALID,
        main,
    )

    ledger = tmp_path / "ledger.md"
    ledger.write_text("\n".join(ROWS) + "\n")
    ids_file = tmp_path / "ids.txt"
    ids_file.write_text(IDS[0] + "\n" + "0" * 64 + "\n")
    monkeypatch.setenv("ONEX_EMIT_EFFECT_SPOOL_DIR", str(tmp_path / "spool"))
    monkeypatch.setenv(
        "ONEX_SKILL_OVERLAY_ROOTS",
        str(
            _overlay_root(
                tmp_path,
                {
                    "backend": "file",
                    "queue_path": str(tmp_path / "queue.jsonl"),
                    "ledger_path": str(ledger),
                    "replay_source": SOURCE,
                },
            )
        ),
    )
    ran = CliRunner().invoke(main, ["--row-ids-file", str(ids_file)])
    report = json.loads(ran.output)
    # Spool-only (no broker): the known row waits as a retry record; the unknown id is named.
    assert ran.exit_code == 1
    assert [p["row_id"] for p in report["still_pending"]] == [IDS[0]]
    assert report["not_in_file"] == ["0" * 64]

    monkeypatch.setenv(
        "ONEX_SKILL_OVERLAY_ROOTS",
        str(_overlay_root(tmp_path / "bare", {"backend": "inmemory"})),
    )
    refused = CliRunner().invoke(main, ["--row-id", IDS[0]])
    assert refused.exit_code == EXIT_OVERLAY_INVALID


def test_an_unknown_bus_lane_is_a_refusal_naming_the_lane() -> None:
    from omnimarket.nodes.node_work_ledger_file_mode_replay_effect.handlers.replay_wiring import (
        bound_bus_lane,
    )

    with (
        pytest.raises(ValueError, match="no-such-lane"),
        bound_bus_lane("no-such-lane", registry_root=None),
    ):
        pass
