# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A non-zero host reconcile exit writes one FRICTION row and one alert on the first failure (OMN-26216)."""

import socket
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from omnimarket.handlers.ledger_row_grammar import parse_row
from omnimarket.models.model_alarm_owner_responder import (
    ModelAlarmOwnerPolicy,
    ModelAlarmOwnerRequest,
)
from omnimarket.nodes.node_alarm_owner_responder_compute.handlers.handler_alarm_owner_responder import (
    HandlerAlarmOwnerResponder,
)
from omnimarket.nodes.node_host_reconcile_effect.handlers.reconcile_failure_alarm import (
    ALARM_ACTOR,
    CONDITION,
)
from tests.test_host_reconcile_effect import (
    RecordingEvaluator,
    RecordingPublisher,
    command,
    handler,
    setup_workspace,
)

pytestmark = pytest.mark.unit


def _writers(root: Path) -> tuple[str, Path, Path]:
    """A bus append command (the row on stdin) and an alert command that each append to a file."""
    ledger = root / "ledger.md"
    writer = root / "writer.sh"
    writer.write_text(f'cat >> "{ledger}"\n')
    alert = root / "alert.sh"
    alerts = root / "alerts.txt"
    alert.write_text('printf "%s\\n---\\n" "$1" >> "' + str(alerts) + '"\n')
    return f"bash {writer}", ledger, alert


def _rows(ledger: Path) -> list[str]:
    return ledger.read_text().splitlines() if ledger.exists() else []


def test_reconcile_failure_alert_on_first_indeterminate_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exit 3 (not a surface failure) used to leave no row and no alert: now one of each, first time."""
    root = setup_workspace(tmp_path, monkeypatch)
    append, ledger, alert = _writers(root)
    cmd = command(
        root,
        target_host=socket.gethostname()
        + "-other",  # setup refuses: INDETERMINATE, exit 3
        ledger_append_command=append,
        alert_command=f"bash {alert}",
    )
    publisher = RecordingPublisher()
    result = handler(RecordingEvaluator(), publisher).handle(cmd)
    assert result.exit_code == 3
    rows = _rows(ledger)
    assert len(rows) == 1
    row = parse_row(rows[0], types=frozenset({"FRICTION"}))
    assert row is not None
    assert row.value("actor") == ALARM_ACTOR
    assert row.value("condition") == CONDITION
    assert row.value("subject") == result.host
    assert row.value("src") == "alarm"
    assert row.value("exit") == "3"
    assert "INDETERMINATE" in rows[0]
    alerts = (root / "alerts.txt").read_text().split("\n---\n")
    assert len([a for a in alerts if a]) == 1
    assert "exited 3" in alerts[0]


def test_reconcile_failure_alert_exit_two_alerts_once_and_writes_one_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A surface failure (exit 2) already alerted from `finish`: the failure row adds no second alert."""
    root = setup_workspace(tmp_path, monkeypatch)
    (root / "omnimarket/.unhealthy").touch()
    append, ledger, alert = _writers(root)
    cmd = command(
        root,
        ledger_append_command=append,
        alert_command=f"bash {alert}",
    )
    result = handler(RecordingEvaluator(), RecordingPublisher()).handle(cmd)
    assert result.exit_code == 2
    assert len(_rows(ledger)) == 1
    alerts = [a for a in (root / "alerts.txt").read_text().split("\n---\n") if a]
    assert len(alerts) == 1


def test_reconcile_failure_alert_success_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Positive control: a clean run writes no row and sends no alert."""
    root = setup_workspace(tmp_path, monkeypatch)
    append, ledger, alert = _writers(root)
    cmd = command(
        root,
        ledger_append_command=append,
        alert_command=f"bash {alert}",
    )
    result = handler(RecordingEvaluator(), RecordingPublisher()).handle(cmd)
    assert result.exit_code == 0
    assert _rows(ledger) == []
    assert not (root / "alerts.txt").exists()


def test_reconcile_failure_alert_writer_failure_keeps_the_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = setup_workspace(tmp_path, monkeypatch)
    cmd = command(
        root,
        target_host=socket.gethostname() + "-other",
        ledger_append_command="false",
    )
    result = handler(RecordingEvaluator(), RecordingPublisher()).handle(cmd)
    assert result.exit_code == 3
    assert any(
        "could not write the failure FRICTION row" in d for d in result.diagnostics
    )


def test_reconcile_failure_alert_row_is_read_by_the_alarm_owner_responder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The row reuses the alarm path: the owner responder dispatches one lane for its condition and host."""
    root = setup_workspace(tmp_path, monkeypatch)
    append, ledger, _ = _writers(root)
    cmd = command(
        root,
        target_host=socket.gethostname() + "-other",
        ledger_append_command=append,
    )
    handler(RecordingEvaluator(), RecordingPublisher()).handle(cmd)
    rows = _rows(ledger)
    raised = datetime.strptime(rows[0].split(" | ")[0], "%Y-%m-%dT%H:%M:%SZ")
    later = (raised + timedelta(hours=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    result = HandlerAlarmOwnerResponder().handle(
        ModelAlarmOwnerRequest(
            now=later,
            rows=tuple(rows),
            policy=ModelAlarmOwnerPolicy(
                alarm_actor=ALARM_ACTOR,
                conditions=(CONDITION,),
                owner_required_after_hours=6,
                redispatch_after_hours=24,
            ),
        )
    )
    assert [d.cause_key for d in result.dispatches] == [
        f"{CONDITION.replace('_', '-')}:{socket.gethostname()}"
    ]
