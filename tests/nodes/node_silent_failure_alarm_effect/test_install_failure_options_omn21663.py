# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The alarm tick's install-failure options: delivery, reraise, and file wiring (OMN-21663).

Error chain: a failed delivery does not persist the raise, an unreadable file is reported, and a
configured-but-missing file warns. Golden chain: the options wire the install-failures file from
the environment and from a JSON config.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
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
    tmp_path: Path,
    rows: list[str],
    run: str = "a",
    *,
    now: datetime = HOST_NOW,
    channel_ok: bool = True,
    write_rows: bool = True,
    rows_as_directory: bool = False,
    **update: object,
) -> tuple[str, list[str], Path, tuple[str, ...]]:
    root = tmp_path / run
    root.mkdir(exist_ok=True)
    sent = root / "sent.txt"
    failures = root / "install-failures.jsonl"
    if write_rows:
        if rows_as_directory:
            failures.mkdir(exist_ok=True)
        else:
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
    if channel_ok:
        (root / "alert.sh").write_text(
            f'alert_channel_send() {{ printf "%s|%s\\n" "$1" "$2" >> {sent}; echo 1234567890.123; }}\n'
        )
    else:
        (root / "alert.sh").write_text("alert_channel_send() { return 1; }\n")
    result = HandlerSilentFailureAlarm(options).handle(
        ModelSilentFailureAlarmRequest(now=now, apply=True)
    )
    texts = sent.read_text().splitlines() if sent.exists() else []
    return result.summary, texts, options.install_failure_state_file, result.warnings


def _row(
    minutes_ago: int, now: datetime = HOST_NOW, detail: str = "installer exit 1"
) -> str:
    stamp = (now - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return json.dumps({"ts": stamp, "detail": detail})


def test_install_failure_a_failed_delivery_does_not_persist_the_raise(
    tmp_path: Path,
) -> None:
    summary, sent, state_file, warnings = _tick(
        tmp_path, [_row(5), _row(65), _row(125)], channel_ok=False
    )
    assert "install_failures=RAISE" in summary
    assert any("install-failure alert not delivered" in w for w in warnings)
    assert (
        not state_file.exists()
        or json.loads(state_file.read_text(encoding="utf-8"))["raise_count"] == 0
    )
    alerts = [line for line in sent if "INSTALL FAILING" in line]
    assert not alerts
    summary2, sent2, state_file2, _ = _tick(
        tmp_path, [_row(5), _row(65), _row(125)], run="b", channel_ok=True
    )
    assert "install_failures=RAISE" in summary2
    alerts2 = [line for line in sent2 if "INSTALL FAILING" in line]
    assert len(alerts2) == 1, sent2
    assert json.loads(state_file2.read_text(encoding="utf-8"))["raise_count"] == 1


def test_install_failure_reraises_after_the_limit_and_counts_up(tmp_path: Path) -> None:
    t1 = HOST_NOW
    t2 = HOST_NOW + timedelta(minutes=100)
    t3 = HOST_NOW + timedelta(minutes=250)
    summary1, sent1, _, _ = _tick(
        tmp_path, [_row(5, t1), _row(65, t1), _row(125, t1)], run="a", now=t1
    )
    assert "install_failures=RAISE" in summary1
    assert len([line for line in sent1 if "INSTALL FAILING" in line]) == 1
    summary2, sent2, _, _ = _tick(
        tmp_path, [_row(5, t2), _row(65, t2), _row(125, t2)], run="b", now=t2
    )
    assert "install_failures=HELD" in summary2
    assert not [line for line in sent2 if "INSTALL FAILING" in line]
    summary3, sent3, state_file3, _ = _tick(
        tmp_path, [_row(5, t3), _row(65, t3), _row(125, t3)], run="c", now=t3
    )
    assert "install_failures=RAISE" in summary3
    alerts3 = [line for line in sent3 if "INSTALL FAILING" in line]
    assert len(alerts3) == 1, sent3
    assert any("raised 2 times" in line for line in alerts3)
    assert json.loads(state_file3.read_text(encoding="utf-8"))["raise_count"] == 2


def test_install_failure_ok_after_a_raise_forgets_it(tmp_path: Path) -> None:
    summary1, _sent1, state_file, _ = _tick(
        tmp_path, [_row(5), _row(65), _row(125)], run="a"
    )
    assert "install_failures=RAISE" in summary1
    assert json.loads(state_file.read_text(encoding="utf-8"))["raise_count"] == 1
    summary2, _sent2, state_file2, _ = _tick(tmp_path, [_row(5)], run="b")
    assert "install_failures=OK" in summary2
    state2 = json.loads(state_file2.read_text(encoding="utf-8"))
    assert state2["raise_count"] == 0
    assert state2["raised_at"] is None
    summary3, sent3, state_file3, _ = _tick(
        tmp_path, [_row(5), _row(65), _row(125)], run="c"
    )
    assert "install_failures=RAISE" in summary3
    alerts3 = [line for line in sent3 if "INSTALL FAILING" in line]
    assert len(alerts3) == 1, sent3
    assert any("raised 1 times" in line for line in alerts3)
    assert json.loads(state_file3.read_text(encoding="utf-8"))["raise_count"] == 1


def test_install_failure_unreadable_file_is_reported_and_keeps_the_state(
    tmp_path: Path,
) -> None:
    summary1, _sent1, state_file, _ = _tick(
        tmp_path, [_row(5), _row(65), _row(125)], run="a"
    )
    assert "install_failures=RAISE" in summary1
    assert json.loads(state_file.read_text(encoding="utf-8"))["raise_count"] == 1
    summary2, sent2, state_file2, _warnings2 = _tick(
        tmp_path, [_row(5), _row(65), _row(125)], run="b", rows_as_directory=True
    )
    assert "install_failures=UNREADABLE" in summary2
    assert not [line for line in sent2 if "INSTALL FAILING" in line]
    assert json.loads(state_file2.read_text(encoding="utf-8"))["raise_count"] == 1


def test_install_failure_a_configured_but_missing_file_warns(tmp_path: Path) -> None:
    summary, sent, _, warnings = _tick(
        tmp_path, [_row(5), _row(65), _row(125)], write_rows=False
    )
    assert "install_failures=OK" in summary
    assert any("does not exist" in w for w in warnings)
    assert not [line for line in sent if "INSTALL FAILING" in line]


def test_install_failure_options_from_environment_wires_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNI_HOME", str(tmp_path))
    monkeypatch.delenv("ONEX_SILENT_FAILURE_ALARM_CONFIG", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    options = ModelSilentFailureAlarmOptions.from_environment()
    assert (
        options.install_failures_file
        == tmp_path / "omni/landing/install-failures.jsonl"
    )
    other = tmp_path / "other.jsonl"
    monkeypatch.setenv("ONEX_INSTALL_FAILURES_FILE", str(other))
    options2 = ModelSilentFailureAlarmOptions.from_environment()
    assert options2.install_failures_file == other


def test_install_failure_options_from_config_json_wires_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "config.json"
    target = tmp_path / "x.jsonl"
    config.write_text(
        json.dumps(
            {"registry_root": str(tmp_path), "install_failures_file": str(target)}
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("ONEX_SILENT_FAILURE_ALARM_CONFIG", str(config))
    monkeypatch.delenv("OMNI_HOME", raising=False)
    options = ModelSilentFailureAlarmOptions.from_environment()
    assert options.install_failures_file == Path(str(target))
