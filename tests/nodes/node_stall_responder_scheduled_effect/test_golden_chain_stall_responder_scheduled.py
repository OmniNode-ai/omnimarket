# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chains of the stall responder on the runtime schedule (OMN-25160).

The ledger rows are the stall responder's own replay fixture of 2026-11-07 (the throughput tick's THROUGHPUT STALL MSG rows).
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.handlers.schedule_gate import ScheduleGate
from omnimarket.handlers.schedule_run_store import InMemoryScheduleRunStore
from omnimarket.models.schedule_trigger.model_schedule_trigger import (
    ModelScheduleDeclaration,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
)
from omnimarket.nodes.node_reading_writer_effect.overlay import (
    ModelCommandLedgerReadRoute,
    ModelFileLedgerReadRoute,
)
from omnimarket.nodes.node_schedule_run_reducer.handlers.handler_schedule_run_reducer import (
    HandlerScheduleRunReducer,
)
from omnimarket.nodes.node_stall_responder_compute.handlers.handler_stall_cause_responder import (
    HandlerStallCauseResponder,
)
from omnimarket.nodes.node_stall_responder_compute.models import (
    ModelStallResponderRequest,
    ModelStallResponderResult,
)
from omnimarket.nodes.node_stall_responder_scheduled_effect.handlers.handler_stall_responder_scheduled import (
    DETAIL_LIMIT,
    TRUNCATED_MARK,
    HandlerStallResponderScheduled,
)
from omnimarket.nodes.node_stall_responder_scheduled_effect.handlers.stall_responder_tick import (
    LedgerRouteError,
    ModelStallResponderTickOutcome,
    run_stall_responder_tick,
)
from omnimarket.nodes.node_stall_responder_scheduled_effect.models import (
    ModelStallResponderScheduleOverlay,
)

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
NODE = "node_stall_responder_scheduled_effect"
FIXTURE = (
    REPO
    / "tests/nodes/node_stall_responder_compute/fixtures/stall_msg_rows_2026-11-07.txt"
)
STALL_ROWS = tuple(FIXTURE.read_text(encoding="utf-8").splitlines())
T0 = datetime(2026, 11, 7, 0, 30, tzinfo=UTC)
DONE = ModelStallResponderTickOutcome(False, "stall_rows=2 dispatches=3")
LEDGER_ENV = "ONEX_STALL_LEDGER_FILE"
FILE_ROUTE: dict[str, Any] = {
    "route": "file",
    "path_env": LEDGER_ENV,
    "timeout_seconds": 120,
    "row_limit": 20000,
}
COMMAND_ROUTE: dict[str, Any] = {
    "route": "command",
    "command_env": "ONEX_LEDGER_BUS_READ_COMMAND",
    "timeout_seconds": 120,
    "row_limit": 20000,
}


def tick(at: datetime) -> ModelRuntimeTick:
    return ModelRuntimeTick(
        now=at,
        tick_id=uuid4(),
        sequence_number=1,
        scheduled_at=at,
        correlation_id=uuid4(),
        scheduler_id="runtime-fixture",
        tick_interval_ms=1000,
    )


def overlay(
    window_seconds: int = 3600,
    claim_lookback_seconds: int = 86400,
    redispatch_after_hours: float = 24,
) -> ModelStallResponderScheduleOverlay:
    return ModelStallResponderScheduleOverlay(
        job_id="stall_responder",
        run_interval_seconds=600,
        grace_seconds=600,
        window_seconds=window_seconds,
        claim_lookback_seconds=claim_lookback_seconds,
        redispatch_after_hours=redispatch_after_hours,
        ledger_read=ModelCommandLedgerReadRoute(
            route="command",
            command_env="ONEX_LEDGER_BUS_READ_COMMAND",
            timeout_seconds=120,
            row_limit=20000,
        ),
    )


def overlay_doc(route: dict[str, Any] | None = None, **changes: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "job_id": "stall_responder",
        "run_interval_seconds": 600,
        "grace_seconds": 600,
        "window_seconds": 3600,
        "claim_lookback_seconds": 86400,
        "redispatch_after_hours": 24,
        "ledger_read": dict(route or COMMAND_ROUTE),
    }
    doc.update(changes)
    return {key: value for key, value in doc.items() if value is not _ABSENT}


_ABSENT = object()


def environ(
    tmp_path: Path, doc: dict[str, Any] | None = None, **extra: str
) -> dict[str, str]:
    """An environment whose overlay root holds the node's overlay.yaml (the command-route one by default)."""
    (tmp_path / NODE).mkdir(exist_ok=True)
    (tmp_path / NODE / "overlay.yaml").write_text(
        yaml.safe_dump(overlay_doc() if doc is None else doc), encoding="utf-8"
    )
    return {"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path), **extra}


def ledger_environ(
    tmp_path: Path, lines: tuple[str, ...] = STALL_ROWS
) -> dict[str, str]:
    """An environment whose overlay reads the ledger file it names: the file route, a readable ledger."""
    ledger = tmp_path / "ledger.md"
    ledger.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return environ(tmp_path, overlay_doc(FILE_ROUTE), **{LEDGER_ENV: str(ledger)})


class _RecordingTick:
    """The injected tick: returns the fixed outcome."""

    def __init__(self, outcome: ModelStallResponderTickOutcome) -> None:
        self._outcome = outcome
        self.calls = 0

    def __call__(self, **kwargs: Any) -> ModelStallResponderTickOutcome:
        self.calls += 1
        return self._outcome


def _raising(error: Exception) -> Any:
    def run(**kwargs: Any) -> ModelStallResponderTickOutcome:
        raise error

    return run


def _errors(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]


def _count(detail: str, name: str) -> int:
    return int(detail.split(f"{name}=")[1].split()[0])


def test_golden_a_due_tick_runs_the_responder_and_returns_its_detail(
    tmp_path: Path,
) -> None:
    event = HandlerStallResponderScheduled(
        environ=environ(tmp_path), tick=_RecordingTick(DONE)
    ).handle(tick(T0))
    assert event is not None
    assert (event.job_id, event.node_name, event.outcome) == (
        "stall_responder",
        NODE,
        "completed",
    )
    assert event.detail == DONE.detail


def test_golden_a_tick_inside_the_interval_returns_none(tmp_path: Path) -> None:
    h = HandlerStallResponderScheduled(
        environ=environ(tmp_path), tick=_RecordingTick(DONE)
    )
    assert h.handle(tick(T0)) is not None
    assert h.handle(tick(T0 + timedelta(seconds=599))) is None
    assert h.handle(tick(T0 + timedelta(seconds=600))) is not None


def test_error_a_failed_pass_is_a_failed_run_with_its_detail(tmp_path: Path) -> None:
    failed = ModelStallResponderTickOutcome(True, "the ledger could not be read: x")
    event = HandlerStallResponderScheduled(
        environ=environ(tmp_path), tick=_RecordingTick(failed)
    ).handle(tick(T0))
    assert event is not None
    assert (event.outcome, event.detail) == ("failed", failed.detail)


def test_error_a_tick_exception_is_a_failed_run_carrying_its_text(
    tmp_path: Path,
) -> None:
    """Any exception after the interval is marked costs the interval, so it must still end in an event."""
    h = HandlerStallResponderScheduled(
        environ=environ(tmp_path), tick=_raising(RuntimeError("kaput: row 17"))
    )
    event = h.handle(tick(T0))
    assert event is not None
    assert event.outcome == "failed"
    assert "kaput: row 17" in event.detail


def test_error_an_unbound_ledger_route_is_a_failed_run_naming_the_variable(
    tmp_path: Path,
) -> None:
    h = HandlerStallResponderScheduled(
        environ=environ(tmp_path),
        tick=_raising(LedgerRouteError("ONEX_LEDGER_BUS_READ_COMMAND is not set")),
    )
    event = h.handle(tick(T0))
    assert event is not None
    assert event.outcome == "failed"
    assert "ledger route of the stall responder is not bound" in event.detail
    assert "ONEX_LEDGER_BUS_READ_COMMAND is not set" in event.detail


def test_error_no_overlay_refuses_once_and_names_the_cause(
    caplog: pytest.LogCaptureFixture,
) -> None:
    h = HandlerStallResponderScheduled(environ={})
    with caplog.at_level(logging.ERROR):
        assert h.handle(tick(T0)) is None
        assert h.handle(tick(T0 + timedelta(seconds=1))) is None
    errors = _errors(caplog)
    assert len(errors) == 1
    assert NODE in errors[0]
    assert "the overlay file is not configured" in errors[0]


def test_error_an_invalid_overlay_file_is_refused_naming_the_cause_through_the_real_gate(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A present overlay that fails validation raises NodeOverlayError from the real gate, not the not-found error."""
    h = HandlerStallResponderScheduled(
        environ=environ(tmp_path, overlay_doc(window_seconds=0)),
        tick=_RecordingTick(DONE),
    )
    with caplog.at_level(logging.ERROR):
        assert h.handle(tick(T0)) is None
        assert h.handle(tick(T0 + timedelta(seconds=1))) is None
    errors = _errors(caplog)
    assert len(errors) == 1
    assert "is invalid for node_stall_responder_scheduled_effect" in errors[0]
    assert "window_seconds" in errors[0]


@pytest.mark.parametrize(
    "field",
    [
        "job_id",
        "run_interval_seconds",
        "grace_seconds",
        "window_seconds",
        "claim_lookback_seconds",
        "redispatch_after_hours",
        "ledger_read",
    ],
)
def test_required_each_overlay_field_is_refused_when_absent_from_the_file(
    field: str, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    recorded = _RecordingTick(DONE)
    h = HandlerStallResponderScheduled(
        environ=environ(tmp_path, overlay_doc(**{field: _ABSENT})), tick=recorded
    )
    with caplog.at_level(logging.ERROR):
        assert h.handle(tick(T0)) is None
    errors = _errors(caplog)
    assert len(errors) == 1
    assert field in errors[0]
    assert recorded.calls == 0


def test_required_the_overlay_model_holds_no_default() -> None:
    for name, info in ModelStallResponderScheduleOverlay.model_fields.items():
        assert info.is_required(), name


@pytest.mark.parametrize(
    ("route_model", "optional"),
    [
        (ModelCommandLedgerReadRoute, set()),
        # archive_subdir is optional in the shared route model: absent, the file route reads no rolls.
        (ModelFileLedgerReadRoute, {"archive_subdir"}),
    ],
)
def test_required_the_nested_ledger_route_models_hold_no_default(
    route_model: type[ModelCommandLedgerReadRoute | ModelFileLedgerReadRoute],
    optional: set[str],
) -> None:
    """The ledger_read route is nested in the overlay: its fields come from the file too, bar the named optional one."""
    defaulted = {
        name
        for name, info in route_model.model_fields.items()
        if not info.is_required()
    }
    assert defaulted == optional


def test_required_the_lookback_must_cover_the_window() -> None:
    with pytest.raises(ValueError, match="claim_lookback_seconds"):
        overlay(window_seconds=7200, claim_lookback_seconds=3600)


def test_seam_the_run_event_folds_into_the_reducer_and_the_topics_match(
    tmp_path: Path,
) -> None:
    event = HandlerStallResponderScheduled(
        environ=environ(tmp_path), tick=_RecordingTick(DONE)
    ).handle(tick(T0))
    assert event is not None
    store = InMemoryScheduleRunStore()
    assert HandlerScheduleRunReducer(store).handle(event).applied is True
    held = store.get("stall_responder")
    assert held is not None
    assert held.last_outcome == "completed"
    producer = yaml.safe_load(
        (REPO / f"src/omnimarket/nodes/{NODE}/contract.yaml").read_text("utf-8")
    )
    reducer = yaml.safe_load(
        (
            REPO / "src/omnimarket/nodes/node_schedule_run_reducer/contract.yaml"
        ).read_text("utf-8")
    )
    assert producer["terminal_event"] == reducer["input_subscriptions"][0]["topic"]


def _run(
    overlay_in: ModelStallResponderScheduleOverlay,
    lines: tuple[str, ...] = STALL_ROWS,
    dispatched: dict[str, str] | None = None,
) -> ModelStallResponderTickOutcome:
    return run_stall_responder_tick(
        overlay=overlay_in,
        dispatched=dispatched or {},
        environ={},
        now=T0,
        ledger=InMemoryLedger(lines),
        responder=HandlerStallCauseResponder(),
    )


def test_real_chain_stall_rows_in_the_window_answer_dispatches() -> None:
    outcome = _run(overlay())
    assert outcome.failed is False
    assert "stall_rows=2 " in outcome.detail
    assert "dispatches=0" not in outcome.detail


def test_control_rows_outside_the_window_are_not_read() -> None:
    """The failing control of the window: a tick that ignored window_seconds would answer the old rows on the narrow run."""
    outcome = _run(overlay(window_seconds=60, claim_lookback_seconds=60))
    assert outcome.failed is False
    assert "stall_rows=0 " in outcome.detail


def test_bound_a_key_dispatched_inside_the_bound_is_not_dispatched_again() -> None:
    first = HandlerStallCauseResponder().handle(
        ModelStallResponderRequest(rows=STALL_ROWS[:2])
    )
    assert first.dispatched
    inside = _run(overlay(redispatch_after_hours=24), dispatched=first.dispatched)
    assert "dispatches=0 " in inside.detail


def test_bound_a_key_dispatched_past_the_bound_is_dispatched_again() -> None:
    """redispatch_after_hours reaches the responder: dropped at the tick it falls back to 24 h and this fails."""
    first = HandlerStallCauseResponder().handle(
        ModelStallResponderRequest(rows=STALL_ROWS[:2])
    )
    past = _run(overlay(redispatch_after_hours=0.1), dispatched=first.dispatched)
    assert past.failed is False
    assert _count(past.detail, "dispatches") > 0


def test_error_an_unreadable_ledger_is_a_failed_pass_not_an_empty_answer() -> None:
    ledger = InMemoryLedger(STALL_ROWS)
    ledger.read_error = "ledger-unreadable:down"
    outcome = run_stall_responder_tick(
        overlay=overlay(),
        dispatched={},
        environ={},
        now=T0,
        ledger=ledger,
        responder=HandlerStallCauseResponder(),
    )
    assert outcome.failed is True
    assert outcome.detail == "the ledger could not be read: ledger-unreadable:down"


def test_control_a_live_lane_covers_its_cause_key() -> None:
    """A CLAIM with no TERMINAL names a live lane; the responder skips the key that lane's stem covers."""
    bare = _run(overlay())
    claim = "2026-11-07T00:09:00Z | CLAIM | lane=stall-floor-omnibase-core-20261107T0009Z | ticket=OMN-20800 | live"
    covered = _run(overlay(), (claim, *STALL_ROWS))
    assert covered.failed is False
    assert "skipped=0 " in bare.detail
    assert "skipped=1 " in covered.detail
    assert _count(covered.detail, "dispatches") == _count(bare.detail, "dispatches") - 1


OLD_CLAIM = "2026-11-06T22:00:00Z | CLAIM | lane=stall-floor-omnibase-core-20261106T2200Z | ticket=OMN-20800 | live"
OLD_TERMINAL = "2026-11-06T23:00:00Z | TERMINAL | lane=stall-floor-omnibase-core-20261106T2200Z | ticket=OMN-20800 | done"


def test_open_claim_before_the_window_still_covers_its_key_over_the_lookback() -> None:
    """A lane claimed two hours before the window and still open is live: its key is skipped, not counted a dispatch."""
    bare = _run(overlay())
    covered = _run(overlay(claim_lookback_seconds=86400), (OLD_CLAIM, *STALL_ROWS))
    assert "skipped=1 " in covered.detail
    assert _count(covered.detail, "dispatches") == _count(bare.detail, "dispatches") - 1


def test_open_claim_older_than_the_lookback_does_not_cover_its_key() -> None:
    narrow = _run(
        overlay(window_seconds=3600, claim_lookback_seconds=3600),
        (OLD_CLAIM, *STALL_ROWS),
    )
    assert "skipped=0 " in narrow.detail


def test_open_claim_with_a_terminal_in_the_lookback_is_not_live() -> None:
    closed = _run(overlay(), (OLD_CLAIM, OLD_TERMINAL, *STALL_ROWS))
    assert "skipped=0 " in closed.detail


REOPEN_CLAIM = "2026-11-06T23:30:00Z | CLAIM | lane=stall-floor-omnibase-core-20261106T2200Z | ticket=OMN-20800 | live again"


def test_claim_terminal_claim_a_lane_claimed_after_its_terminal_is_live() -> None:
    """A TERMINAL closes a lane only when no later CLAIM reopens it: claim, terminal, claim is a live lane."""
    bare = _run(overlay())
    reopened = _run(overlay(), (OLD_CLAIM, OLD_TERMINAL, REOPEN_CLAIM, *STALL_ROWS))
    assert "skipped=1 " in reopened.detail
    assert (
        _count(reopened.detail, "dispatches") == _count(bare.detail, "dispatches") - 1
    )


def test_claim_terminal_claim_a_terminal_after_the_last_claim_closes_the_lane() -> None:
    late_terminal = "2026-11-06T23:45:00Z | TERMINAL | lane=stall-floor-omnibase-core-20261106T2200Z | ticket=OMN-20800 | done"
    closed = _run(
        overlay(), (OLD_CLAIM, OLD_TERMINAL, REOPEN_CLAIM, late_terminal, *STALL_ROWS)
    )
    assert "skipped=0 " in closed.detail


def test_keys_are_not_stored_between_runs_so_a_standing_stall_reports_again(
    tmp_path: Path,
) -> None:
    """Nothing starts a lane, so a dispatched key must not suppress the next run's count while the stall stands."""
    h = HandlerStallResponderScheduled(environ=ledger_environ(tmp_path))
    first = h.handle(tick(T0))
    second = h.handle(tick(T0 + timedelta(seconds=600)))
    assert first is not None
    assert second is not None
    assert first.outcome == second.outcome == "completed"
    assert _count(first.detail, "dispatches") > 0
    assert _count(second.detail, "dispatches") >= _count(first.detail, "dispatches")


def test_default_tick_file_overlay_over_a_readable_ledger_completes(
    tmp_path: Path,
) -> None:
    """The handler's own call into the real tick: wrong argument names or a skipped build_row_source fail here."""
    event = HandlerStallResponderScheduled(environ=ledger_environ(tmp_path)).handle(
        tick(T0)
    )
    assert event is not None
    assert event.outcome == "completed"
    assert "stall_rows=2 " in event.detail
    assert _count(event.detail, "dispatches") > 0


def test_default_tick_unset_command_env_is_a_failed_run_naming_it(
    tmp_path: Path,
) -> None:
    """The command route's variable is unset: build_row_source raises LedgerRouteError, which becomes a failed event."""
    event = HandlerStallResponderScheduled(environ=environ(tmp_path)).handle(tick(T0))
    assert event is not None
    assert event.outcome == "failed"
    assert "ONEX_LEDGER_BUS_READ_COMMAND is not set" in event.detail


def test_default_tick_environ_none_reads_the_process_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """environ=None falls back to os.environ for the gate and for the ledger route."""
    for key, value in ledger_environ(tmp_path).items():
        monkeypatch.setenv(key, value)
    event = HandlerStallResponderScheduled().handle(tick(T0))
    assert event is not None
    assert event.outcome == "completed"
    assert "stall_rows=2 " in event.detail


def test_error_a_malformed_yaml_overlay_is_refused_naming_the_cause_through_the_real_gate(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Malformed YAML: the loader raises NodeOverlayError naming YAMLError, refused once, never raised out of handle()."""
    (tmp_path / NODE).mkdir()
    (tmp_path / NODE / "overlay.yaml").write_text(
        "job_id: [stall_responder\nrun_interval_seconds: 600\n", encoding="utf-8"
    )
    recorded = _RecordingTick(DONE)
    h = HandlerStallResponderScheduled(
        environ={"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)}, tick=recorded
    )
    with caplog.at_level(logging.ERROR):
        assert h.handle(tick(T0)) is None
        assert h.handle(tick(T0 + timedelta(seconds=1))) is None
    errors = _errors(caplog)
    assert len(errors) == 1
    assert "is unreadable (" in errors[0]
    assert "YAMLError" in errors[0] or "ParserError" in errors[0]
    assert recorded.calls == 0


def test_error_an_unreadable_overlay_file_is_refused_naming_the_cause_through_the_real_gate(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A file that cannot be decoded: the loader raises NodeOverlayError naming the decode error, refused once."""
    (tmp_path / NODE).mkdir()
    (tmp_path / NODE / "overlay.yaml").write_bytes(b"job_id: \xff\xfe\xfa\n")
    h = HandlerStallResponderScheduled(
        environ={"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)}, tick=_RecordingTick(DONE)
    )
    with caplog.at_level(logging.ERROR):
        assert h.handle(tick(T0)) is None
    errors = _errors(caplog)
    assert len(errors) == 1
    assert "is unreadable (UnicodeDecodeError)" in errors[0]


def test_error_unreadable_overlay_roots_are_refused_by_name_not_raised(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A root that cannot be searched makes is_file raise OSError before the loader's read catch; handle() refuses it."""
    unsearchable = str(tmp_path / ("r" * 300))
    h = HandlerStallResponderScheduled(
        environ={"ONEX_SKILL_OVERLAY_ROOTS": unsearchable}, tick=_RecordingTick(DONE)
    )
    with caplog.at_level(logging.ERROR):
        assert h.handle(tick(T0)) is None
        assert h.handle(tick(T0 + timedelta(seconds=1))) is None
    errors = _errors(caplog)
    assert len(errors) == 1
    assert "the overlay roots cannot be searched (OSError)" in errors[0]


def test_error_a_gate_declaration_of_another_model_is_refused_by_name(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """H: the isinstance guard: a gate holding a plain schedule declaration is refused, the tick never runs."""
    plain = ModelScheduleDeclaration(
        job_id="stall_responder", run_interval_seconds=600, grace_seconds=600
    )
    recorded = _RecordingTick(DONE)
    h = HandlerStallResponderScheduled(
        gate=ScheduleGate(NODE, declaration=plain), tick=recorded
    )
    with caplog.at_level(logging.ERROR):
        assert h.handle(tick(T0)) is None
    errors = _errors(caplog)
    assert len(errors) == 1
    assert "the overlay is not a stall responder schedule overlay" in errors[0]
    assert recorded.calls == 0


def test_long_error_a_long_ledger_route_error_is_truncated_in_the_event(
    tmp_path: Path,
) -> None:
    long_text = "x" * 5000
    event = HandlerStallResponderScheduled(
        environ=environ(tmp_path), tick=_raising(LedgerRouteError(long_text))
    ).handle(tick(T0))
    assert event is not None
    assert event.outcome == "failed"
    assert len(event.detail) == DETAIL_LIMIT
    assert event.detail.endswith(TRUNCATED_MARK)
    assert "ledger route of the stall responder is not bound" in event.detail


def test_long_error_a_long_failed_pass_detail_is_truncated_in_the_event(
    tmp_path: Path,
) -> None:
    failed = ModelStallResponderTickOutcome(
        True, "the ledger could not be read: " + "y" * 5000
    )
    event = HandlerStallResponderScheduled(
        environ=environ(tmp_path), tick=_RecordingTick(failed)
    ).handle(tick(T0))
    assert event is not None
    assert len(event.detail) == DETAIL_LIMIT
    assert event.detail.endswith(TRUNCATED_MARK)


def test_long_error_a_short_detail_is_not_cut(tmp_path: Path) -> None:
    event = HandlerStallResponderScheduled(
        environ=environ(tmp_path), tick=_RecordingTick(DONE)
    ).handle(tick(T0))
    assert event is not None
    assert event.detail == DONE.detail


class _RecordingResponder(HandlerStallCauseResponder):
    def __init__(self) -> None:
        super().__init__()
        self.requests: list[ModelStallResponderRequest] = []

    def handle(self, request: ModelStallResponderRequest) -> ModelStallResponderResult:
        self.requests.append(request)
        return super().handle(request)


def test_injected_responder_is_the_one_the_default_tick_asks(tmp_path: Path) -> None:
    """The responder is injected through the constructor keyword; the real tick asks that instance."""
    responder = _RecordingResponder()
    event = HandlerStallResponderScheduled(
        environ=ledger_environ(tmp_path), responder=responder
    ).handle(tick(T0))
    assert event is not None
    assert event.outcome == "completed"
    assert len(responder.requests) == 1
    assert len(responder.requests[0].rows) == 2


_STUB_COMMAND = """
import json, sys
request = json.loads(sys.stdin.read())
with open(sys.argv[1], "a", encoding="utf-8") as log:
    log.write(json.dumps(request) + "\\n")
answer = json.loads(open(sys.argv[2], encoding="utf-8").read())
rows = [
    {"raw_row": line}
    for line in answer["lines"]
    if f" | {request['kinds'][0]} | " in line
    and (request.get("term") is None or request["term"] in line)
]
out = {"status": "ok", "rows": rows}
if answer.get("matched_extra"):
    out["matched"] = len(rows) + answer["matched_extra"]
print(json.dumps(out))
"""


def _command_environ(
    tmp_path: Path, lines: tuple[str, ...], matched_extra: int = 0
) -> tuple[dict[str, str], Path]:
    stub = tmp_path / "stub_read.py"
    stub.write_text(_STUB_COMMAND, encoding="utf-8")
    answer = tmp_path / "answer.json"
    answer.write_text(
        json.dumps({"lines": list(lines), "matched_extra": matched_extra}),
        encoding="utf-8",
    )
    requests = tmp_path / "requests.jsonl"
    command = f"{sys.executable} {stub} {requests} {answer}"
    return (
        environ(tmp_path, overlay_doc(), ONEX_LEDGER_BUS_READ_COMMAND=command),
        requests,
    )


def test_command_route_the_production_route_reads_through_a_stub_command(
    tmp_path: Path,
) -> None:
    """The command route (the production one): the handler's own tick sends one typed query per row type to the command."""
    env, requests = _command_environ(tmp_path, STALL_ROWS)
    event = HandlerStallResponderScheduled(environ=env).handle(tick(T0))
    assert event is not None
    assert event.outcome == "completed", event.detail
    assert "stall_rows=2 " in event.detail
    sent = [
        json.loads(line) for line in requests.read_text(encoding="utf-8").splitlines()
    ]
    assert [q["kinds"] for q in sent] == [["MSG"], ["CLAIM"], ["TERMINAL"]]
    assert sent[0]["term"] == "THROUGHPUT STALL"
    assert all(q["limit"] == 20000 for q in sent)


def test_command_route_a_read_past_its_row_limit_is_a_failed_run(
    tmp_path: Path,
) -> None:
    """A read that reports more matches than rows (row_limit reached) is read-answer-truncated: a failed run, not short."""
    env, _ = _command_environ(tmp_path, STALL_ROWS, matched_extra=5)
    event = HandlerStallResponderScheduled(environ=env).handle(tick(T0))
    assert event is not None
    assert event.outcome == "failed"
    assert "read-answer-truncated" in event.detail
