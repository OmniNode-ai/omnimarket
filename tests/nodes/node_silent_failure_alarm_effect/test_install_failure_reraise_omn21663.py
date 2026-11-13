# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The alarm tick reads install-failures rows and raises a repeating failure (OMN-21663).

Error chain: an unconfigured file is reported and never a pass, and an unreadable row is counted as a warning.
Golden chain: rows past the configured threshold produce one alert and a state file that holds the raise.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from omnimarket.nodes.node_silent_failure_alarm_effect.handlers.handler_silent_failure_alarm import (
    HandlerSilentFailureAlarm,
)
from omnimarket.nodes.node_silent_failure_alarm_effect.models import (
    ModelSilentFailureAlarmOptions,
    ModelSilentFailureAlarmRequest,
)
from tests.nodes.node_silent_failure_alarm_compute.test_golden_chain_silent_failure_alarm_compute import (
    HOST_NOW,
    world,
)

pytestmark = pytest.mark.unit


def _tick(
    tmp_path: Path, rows: list[str], run: str = "a", **update: object
) -> tuple[str, list[str], Path, tuple[str, ...]]:
    root = tmp_path / run
    root.mkdir()
    sent = root / "sent.txt"
    failures = root / "install-failures.jsonl"
    failures.write_text("\n".join(rows) + "\n", encoding="utf-8")
    options: ModelSilentFailureAlarmOptions = world(root).model_copy(
        update={
            "install_failures_file": failures,
            "install_failure_state_file": tmp_path / "install-failure-state.json",
            "install_failure_threshold": 3,
            "install_failure_window_minutes": 360,
            "install_failure_reraise_minutes": 240,
            **update,
        }
    )
    (root / "alert.sh").write_text(
        f'alert_channel_send() {{ printf "%s|%s\\n" "$1" "$2" >> {sent}; echo 1234567890.123; }}\n'
    )
    result = HandlerSilentFailureAlarm(options).handle(
        ModelSilentFailureAlarmRequest(now=HOST_NOW, apply=True)
    )
    texts = sent.read_text().splitlines() if sent.exists() else []
    return result.summary, texts, options.install_failure_state_file, result.warnings


def _row(minutes_ago: int, detail: str = "installer exit 1") -> str:
    stamp = (HOST_NOW - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return json.dumps({"ts": stamp, "detail": detail})


def test_install_failure_repeating_rows_raise_once_then_hold(tmp_path: Path) -> None:
    summary, sent, state_file, _ = _tick(tmp_path, [_row(5), _row(65), _row(125)])
    alerts = [line for line in sent if "INSTALL FAILING" in line]
    assert len(alerts) == 1, sent
    assert alerts[0].startswith("install_failure_alarm|")
    assert "install_failures=RAISE" in summary
    assert json.loads(state_file.read_text(encoding="utf-8"))["raise_count"] == 1
    summary, sent2, _, _ = _tick(tmp_path, [_row(5), _row(65), _row(125)], run="b")
    assert "install_failures=HELD" in summary
    assert not [line for line in sent2 if "INSTALL FAILING" in line]


def test_install_failure_below_the_threshold_raises_nothing(tmp_path: Path) -> None:
    summary, sent, _, _ = _tick(tmp_path, [_row(5), _row(65)])
    assert "install_failures=OK" in summary
    assert not [line for line in sent if "INSTALL FAILING" in line]


def test_install_failure_no_file_configured_is_unconfigured_not_ok(
    tmp_path: Path,
) -> None:
    summary, _, _, _ = _tick(tmp_path, [_row(5)], install_failures_file=None)
    assert "install_failures=UNCONFIGURED" in summary


def test_install_failure_a_row_that_is_not_json_is_a_warning_and_is_not_counted(
    tmp_path: Path,
) -> None:
    summary, sent, _, warnings = _tick(tmp_path, [_row(5), "not json", _row(65)])
    assert "install_failures=OK" in summary
    assert any("install-failures row" in warning for warning in warnings)
    assert not [line for line in sent if "INSTALL FAILING" in line]


def test_install_failure_options_refuse_a_zero_threshold() -> None:
    with pytest.raises(ValueError, match="install_failure_threshold"):
        ModelSilentFailureAlarmOptions(
            registry_root=Path("/tmp/x"), install_failure_threshold=0
        )


def test_install_failure_seam_rows_as_the_controller_writes_them_are_counted(
    tmp_path: Path,
) -> None:
    """Seam with the landing controller's install-failures file: ``ts`` in its stamp format, ``detail``, and
    ``class`` on a refusal. The shapes are the controller's own (its writer appends ``{"ts", "detail"}`` and adds
    ``"class": "tlc-receipt-missing"`` for a missing receipt); a change to either side's keys turns this red."""
    rows = [
        json.dumps(
            {
                "ts": (HOST_NOW - timedelta(minutes=m)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "detail": "x",
                **extra,
            }
        )
        for m, extra in ((5, {}), (65, {"class": "tlc-receipt-missing"}), (125, {}))
    ]
    summary, sent, _, warnings = _tick(tmp_path, rows)
    assert "install_failures=RAISE" in summary
    assert not [w for w in warnings if "install-failures row" in w]
    assert len([line for line in sent if "INSTALL FAILING 3 times" in line]) == 1
