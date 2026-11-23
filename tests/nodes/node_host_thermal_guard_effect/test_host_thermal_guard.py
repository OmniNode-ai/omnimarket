# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_host_thermal_guard_effect (OMN-24562): hysteresis, the declared hold, the ledger rows and the receipt client.

The guard this node replaces had one threshold, chmodded the run folder and wrote its rows through a client pinned to an old
receipt model, so every row failed with exit 70 and nothing recorded the hold.
"""

from __future__ import annotations

import json
import stat
import sys
import uuid
from pathlib import Path

import pytest

from omnimarket.models.work_ledger_append.model_work_ledger_append import (
    EnumWorkLedgerAppendStatus,
    ModelWorkLedgerAppendReceipt,
)
from omnimarket.nodes.node_host_thermal_guard_effect.adapters import (
    FileEpisodeStore,
    FileHoldStore,
    InMemoryLaneCutter,
    InMemorySensors,
)
from omnimarket.nodes.node_host_thermal_guard_effect.handlers import (
    HandlerHostThermalGuard,
    decide_action,
)
from omnimarket.nodes.node_host_thermal_guard_effect.models import (
    EnumGuardAction,
    ModelHostThermalGuardRequest,
    ModelThermalReading,
)
from omnimarket.nodes.node_host_thermal_guard_effect.overlay import (
    ModelHostThermalGuardOverlay,
)
from omnimarket.nodes.node_ledger_row_grammar_compute.handlers.handler_ledger_row_grammar import (
    HandlerLedgerRowGrammar,
)
from omnimarket.nodes.node_ledger_row_grammar_compute.models.model_ledger_row_grammar import (
    ModelLedgerRowGrammarRequest,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    CommandLedgerAppender,
)

from .conftest import NODE, Rig, example_overlay_text

pytestmark = pytest.mark.unit

NODE_DIR = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_host_thermal_guard_effect"
)


def _refusals(row: str) -> list[str]:
    return (
        HandlerLedgerRowGrammar()
        .handle(ModelLedgerRowGrammarRequest(operation="row_refusals", line=row))
        .refusals
    )


# --- hysteresis -----------------------------------------------------------------------------------------------------------


def test_a_reading_that_oscillates_around_the_hold_threshold_holds_once_and_never_releases(
    rig: Rig,
) -> None:
    """The old guard held at 85.5, released at 84.5 and held again at 85.5 (one threshold, no gap)."""
    first = rig.run(85.5)
    second = rig.run(84.5, after_s=60)
    third = rig.run(85.5, after_s=60)
    assert [first.action, second.action, third.action] == ["hold", "none", "none"]
    assert [r.held for r in (first, second, third)] == [True, True, True]
    sent = [rows for _, rows in rig.ledger.sent]
    assert len(sent) == 1
    assert " | HOLD | " in sent[0]
    assert not any(" | RELEASE | " in rows for rows in sent)


def test_a_reading_between_the_two_thresholds_never_releases_even_after_the_minimum_hold(
    rig: Rig,
) -> None:
    rig.run(86.0)
    result = rig.run(83.0, after_s=rig.overlay.min_hold_s + 600)
    assert result.action == "none"
    assert result.held


def test_the_release_needs_a_reading_below_the_release_threshold_and_the_minimum_hold(
    rig: Rig,
) -> None:
    rig.run(86.0)
    too_early = rig.run(70.0, after_s=rig.overlay.min_hold_s - 1)
    assert too_early.action == "none"
    assert too_early.held
    released = rig.run(70.0, after_s=2)
    assert released.action == "release"
    assert not released.held
    assert rig.holds.record is None


def test_the_overlay_refuses_a_release_threshold_that_is_not_below_the_hold_threshold(
    overlay: ModelHostThermalGuardOverlay,
) -> None:
    data = overlay.model_dump()
    data["release_below_c"] = data["hold_above_c"]
    with pytest.raises(ValueError, match="release_below_c must be below hold_above_c"):
        ModelHostThermalGuardOverlay.model_validate(data)


@pytest.mark.parametrize(
    ("held", "hottest", "waited", "expected"),
    [
        (False, 85.0, 0, EnumGuardAction.NONE),
        (False, 85.1, 0, EnumGuardAction.HOLD),
        (True, 82.0, 999, EnumGuardAction.NONE),
        (True, 81.9, 299, EnumGuardAction.NONE),
        (True, 81.9, 300, EnumGuardAction.RELEASE),
    ],
)
def test_decide_action_table(held, hottest, waited, expected) -> None:
    from datetime import UTC, datetime, timedelta

    now = datetime(2026, 11, 23, 21, 0, 0, tzinfo=UTC)
    since = now - timedelta(seconds=waited) if held else None
    assert decide_action(held, since, hottest, now, 85.0, 82.0, 300) is expected


def test_a_gpu_reading_holds_the_host_when_the_cpu_is_cool(rig: Rig) -> None:
    result = rig.run(88.0, gpu=True)
    assert result.action == "hold"
    assert result.hottest_c == 88.0


# --- the declared hold, never a chmod -------------------------------------------------------------------------------------


def test_no_source_file_of_the_node_changes_the_mode_of_a_folder() -> None:
    offenders = [
        f"{path.name}:{number}"
        for path in NODE_DIR.rglob("*.py")
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if "chmod" in line
    ]
    assert offenders == []


def test_a_hold_writes_the_hold_file_and_leaves_the_run_folder_mode_as_it_is(
    tmp_path: Path, overlay: ModelHostThermalGuardOverlay
) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()
    runs.chmod(0o755)
    guard_dir = tmp_path / "guard"
    handler = HandlerHostThermalGuard(
        InMemorySensors(ModelThermalReading("", 90.0, ())),
        FileHoldStore(guard_dir, overlay.hold_file),
        FileEpisodeStore(guard_dir, overlay.episode_file),
        __import__(f"{NODE}.adapters", fromlist=["x"]).InMemoryLedgerAppender(),
        InMemoryLaneCutter(),
    )
    result = handler.handle(ModelHostThermalGuardRequest(host="lab-a", overlay=overlay))
    assert result.action == "hold"
    record = json.loads((guard_dir / overlay.hold_file).read_text())
    assert record["host"] == "lab-a"
    assert record["since"]
    assert record["hold_id"].startswith(record["since"])
    assert stat.S_IMODE(runs.stat().st_mode) == 0o755


def test_an_unreadable_hold_file_is_an_error_and_never_a_release(rig: Rig) -> None:
    rig.holds.unreadable = "hold.json: not JSON"
    result = rig.run(20.0, after_s=10_000)
    assert result.action == "none"
    assert not result.ok
    assert result.errors[0][0] == "hold_unreadable"
    assert rig.ledger.sent == []


def test_no_sensor_reading_changes_nothing_and_says_so(rig: Rig) -> None:
    rig.run(86.0)
    result = rig.run(None, after_s=10_000)
    assert result.held
    assert result.action == "none"
    assert result.errors[0][0] == "no_sensor_reading"
    assert not result.ok


def test_a_hold_that_cannot_be_written_queues_no_row_that_says_it_was_made(
    rig: Rig,
) -> None:
    rig.holds.write_error = PermissionError("read-only")
    result = rig.run(90.0)
    assert not result.held
    assert not result.ok
    assert rig.ledger.sent == []
    assert result.errors[0][0] == "hold_unwritable"


def test_a_dry_run_writes_nothing_and_sends_nothing(rig: Rig) -> None:
    result = rig.run(90.0, dry_run=True)
    assert result.action == "hold"
    assert len(result.rows) == 1
    assert rig.holds.record is None
    assert rig.ledger.sent == []
    assert rig.episodes.record == {}


# --- the ledger rows -------------------------------------------------------------------------------------------------------


def test_the_hold_and_release_rows_name_host_reading_thresholds_and_actor_and_pass_the_row_grammar(
    rig: Rig,
) -> None:
    held = rig.run(86.5)
    released = rig.run(70.0, after_s=rig.overlay.min_hold_s + 1)
    hold_row, release_row = held.rows[0], released.rows[0]
    for row in (hold_row, release_row):
        for cell in (
            "host=lab-a",
            "hold_above_c=85",
            "release_below_c=82",
            f"actor={rig.overlay.actor}",
        ):
            assert cell in row
    assert "hottest_c=86.5" in hold_row
    assert "hottest_c=70.0" in release_row
    assert f"| id={held.hold_id} |" in hold_row
    assert "surface=thermal-lab-a" in hold_row
    assert "until=" in hold_row
    assert f"| re={held.hold_id} |" in release_row
    assert "result=PASS" in release_row
    assert "restored=yes" in release_row
    assert len(rig.ledger.sent) == 2


def test_the_row_grammar_check_refuses_a_hold_with_no_release_condition(
    rig: Rig,
) -> None:
    """Positive control: the check below is not a check that passes anything."""
    held = rig.run(86.5)
    no_until = " | ".join(
        c for c in held.rows[0].split(" | ") if not c.startswith("until=")
    )
    assert _refusals(f"{no_until} | req={uuid.uuid4()}") != []


def test_the_rows_as_the_bus_append_gets_them_pass_the_ledger_row_grammar(
    rig: Rig,
) -> None:
    held = rig.run(86.5)
    released = rig.run(70.0, after_s=rig.overlay.min_hold_s + 1)
    for row in (held.rows[0], released.rows[0]):
        assert _refusals(f"{row} | req={uuid.uuid4()}") == []


def test_a_row_that_was_not_delivered_stays_queued_and_is_sent_again_under_the_same_request_id(
    rig: Rig,
) -> None:
    rig.ledger.default = "error"
    first = rig.run(90.0)
    assert not first.ok
    assert first.delivered == 0
    assert first.pending == 1
    assert first.errors[0][0] == "row_not_delivered"
    request_id = rig.ledger.sent[0][0]
    rig.ledger.default = "accepted"
    second = rig.run(90.0, after_s=60)
    assert second.ok
    assert second.delivered == 1
    assert second.pending == 0
    assert [rid for rid, _ in rig.ledger.sent] == [request_id, request_id]
    third = rig.run(90.0, after_s=60)
    assert third.delivered == 0
    assert len(rig.ledger.sent) == 2


def test_a_refused_row_is_reported_and_kept_in_the_episode_not_retried(
    rig: Rig,
) -> None:
    rig.ledger.default = "refused"
    result = rig.run(90.0)
    assert not result.ok
    assert len(result.refused) == 1
    assert result.pending == 0
    assert rig.episodes.record is not None
    assert len(rig.episodes.record["refused"]) == 1
    assert len(rig.ledger.sent) == 1
    assert len(rig.run(90.0, after_s=60).refused) == 0


def test_a_stop_reading_cuts_the_lanes_once_per_report_window_and_says_so(
    rig: Rig,
) -> None:
    first = rig.run(91.0)
    assert first.cut == ("onexlane-test.scope",)
    assert rig.cutter.cuts == 1
    assert any("thermal stop" in row for row in first.rows)
    again = rig.run(91.0, after_s=60)
    assert again.cut == ()
    assert rig.cutter.cuts == 1
    later = rig.run(91.0, after_s=rig.overlay.stop_report_every_s)
    assert rig.cutter.cuts == 2
    assert later.cut


# --- the receipt client ---------------------------------------------------------------------------------------------------


def _fake_bus_append(tmp_path: Path, exit_code: int, receipt: dict | None) -> list[str]:
    script = tmp_path / "append.py"
    script.write_text(
        "import json,sys\n"
        f"receipt = json.loads({json.dumps(json.dumps(receipt))})\n"
        "print(json.dumps(receipt))\n"
        f"sys.exit({exit_code})\n"
    )
    return [sys.executable, str(script)]


def _current_receipt(request_id: str) -> dict:
    return json.loads(
        ModelWorkLedgerAppendReceipt(
            request_id=uuid.UUID(request_id),
            status=EnumWorkLedgerAppendStatus.ACCEPTED,
            exit_code=0,
            message="appended",
            ledger_lines=[],
            ledger_host="h202",
            duration_ms=12,
            principal="h202-dev-runtime",
            ledger_seq=41,
        ).model_dump_json()
    )


def test_a_receipt_carrying_a_principal_field_reads_as_delivered(
    tmp_path: Path, overlay: ModelHostThermalGuardOverlay
) -> None:
    """The old client's receipt model refused the principal field: every row exited 70 and none counted."""
    request_id = str(uuid.uuid4())
    receipt = _current_receipt(request_id)
    assert receipt["principal"] == "h202-dev-runtime"
    appender = CommandLedgerAppender(
        _fake_bus_append(tmp_path, 0, receipt), "guard-lane", "h202", 30
    )
    row = "2026-11-23T21:00:00Z | HOLD | lane=guard-lane | id=2026-11-23T21:00:00Z-guard-lane | surface=thermal-h202 | until=2026-11-24T03:00:00Z | note"
    assert appender.append(row, request_id).status == "accepted"


def test_a_client_that_exits_70_reads_as_not_delivered_and_the_row_stays_queued(
    tmp_path: Path, overlay: ModelHostThermalGuardOverlay
) -> None:
    failing = CommandLedgerAppender(
        _fake_bus_append(
            tmp_path,
            70,
            {"status": "error", "exit_code": 70, "message": "unreadable receipt"},
        ),
        "guard-lane",
        "lab-a",
        30,
    )
    rig = Rig(overlay)
    rig.ledger = failing  # type: ignore[assignment]
    result = rig.run(90.0)
    assert not result.ok
    assert result.delivered == 0
    assert result.pending == 1
    assert result.errors[0][0] == "row_not_delivered"
    assert len(rig.episodes.record["pending"]) == 1  # type: ignore[index]


# --- the example overlay and the tick ------------------------------------------------------------------------------------


def test_the_example_overlay_loads_and_carries_every_threshold_as_data(
    overlay: ModelHostThermalGuardOverlay,
) -> None:
    assert (overlay.hold_above_c, overlay.release_below_c, overlay.min_hold_s) == (
        85.0,
        82.0,
        300,
    )
    text = example_overlay_text()
    assert "release_below_c" in text
    assert "min_hold_s" in text


def test_the_tick_simulation_shows_a_hold_then_a_release_with_both_hold_states(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    from omnimarket.nodes.node_host_thermal_guard_effect import (
        runtime_host_thermal_guard_tick as tick,
    )

    root = tmp_path / "overlays" / "node_host_thermal_guard_effect"
    root.mkdir(parents=True)
    (root / "overlay.yaml").write_text(example_overlay_text(), encoding="utf-8")
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path / "overlays"))
    code = tick.main(["--host", "lab-a", "--simulate-readings", "90,84,70"])
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert code == 0
    assert [(x["action"], x["held"]) for x in lines] == [
        ("hold", True),
        ("none", True),
        ("release", False),
    ]
    assert " | HOLD | " in lines[0]["rows"][0]
    assert " | RELEASE | " in lines[2]["rows"][0]


def test_the_tick_probe_answers_without_loading_an_overlay(capsys) -> None:
    from omnimarket.nodes.node_host_thermal_guard_effect import (
        runtime_host_thermal_guard_tick as tick,
    )

    assert tick.main(["--probe"]) == 0
    assert json.loads(capsys.readouterr().out) == {"status": "probe_ok"}
