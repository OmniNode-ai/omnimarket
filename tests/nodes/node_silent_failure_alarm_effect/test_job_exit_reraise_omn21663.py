# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The alarm tick raises an old open job-exit episode again, with the limit from its options (OMN-21663).

The recorded case: ``lab-alarm-broker-lag`` kept its episode open from 2026-11-08 and the tick said nothing after
the first push. These tests run the real effect handler against a stand-in ``launchctl`` that reports the job's
last exit as 2 on the run count already recorded, and a stand-in alert channel that writes what it is asked to send.
"""

from __future__ import annotations

import json
import sys
from datetime import timedelta
from pathlib import Path

import pytest

from omnimarket.models.silent_failure_alarm import ModelJobExitStreak
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

LABEL = "ai.omninode.lab-alarm-broker-lag"
EPISODE = "ep-20261108T1853Z-lab-alarm-broker-lag"


def _launchctl(tmp_path: Path) -> Path:
    script = tmp_path / "launchctl"
    script.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        "if sys.argv[1] == 'list':\n"
        f"    print('-\\t2\\t{LABEL}')\n"
        "else:\n"
        "    print('\\truns = 40')\n"
        "    print('\\tlast exit code = 2')\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def _run(tmp_path: Path, **update: object) -> tuple[str, list[str], Path]:
    sent = tmp_path / "sent.txt"
    opened = HOST_NOW - timedelta(days=4)
    state = ModelJobExitStreak(
        label=LABEL,
        runs=40,
        last_exit=2,
        streak=40,
        episode=EPISODE,
        opened_at=opened,
        pushed=True,
        cause="failed-exit",
        last_run_at=HOST_NOW - timedelta(minutes=10),
        raised_at=opened + timedelta(minutes=30),
        raise_count=1,
    )
    options: ModelSilentFailureAlarmOptions = world(tmp_path).model_copy(
        update={
            "launchctl": str(_launchctl(tmp_path)),
            "brief_dir": tmp_path / "briefs",
            **update,
        }
    )
    (tmp_path / "alert.sh").write_text(
        f'alert_channel_send() {{ printf "%s|%s\\n" "$1" "$2" >> {sent}; echo 1234567890.123; }}\n'
    )
    options.job_state_file.write_text(
        json.dumps({"streaks": [state.model_dump(mode="json")]}), encoding="utf-8"
    )
    result = HandlerSilentFailureAlarm(options).handle(
        ModelSilentFailureAlarmRequest(now=HOST_NOW, apply=True)
    )
    texts = sent.read_text().splitlines() if sent.exists() else []
    return result.summary, texts, options.job_state_file


def test_job_exit_reraise_old_open_episode_sends_an_alert_naming_age_and_count(
    tmp_path: Path,
) -> None:
    summary, sent, state_file = _run(tmp_path, job_reraise_after_minutes=240)
    again = [line for line in sent if "STILL OPEN" in line]
    assert len(again) == 1, sent
    assert again[0].startswith("job_exit_alarm|")
    assert f"episode {EPISODE}" in again[0]
    assert "open 5760 minutes" in again[0]
    assert "raised 2 times" in again[0]
    assert "reraised=1" in summary
    saved = json.loads(state_file.read_text(encoding="utf-8"))["streaks"][0]
    assert saved["raise_count"] == 2


def test_job_exit_reraise_off_at_zero_sends_nothing_for_the_same_state(
    tmp_path: Path,
) -> None:
    summary, sent, _ = _run(tmp_path, job_reraise_after_minutes=0)
    assert not [line for line in sent if "STILL OPEN" in line]
    assert "reraised=0" in summary


def test_job_exit_reraise_limit_is_an_options_field_with_a_floor_of_zero() -> None:
    with pytest.raises(ValueError, match="job_reraise_after_minutes"):
        ModelSilentFailureAlarmOptions(
            registry_root=Path("/tmp/x"), job_reraise_after_minutes=-1
        )
