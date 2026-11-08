"""The shard plan records a duration for the host table test file (OMN-22755)."""

from __future__ import annotations

import json
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
_TEST_FILE = (
    "tests/nodes/node_silent_failure_alarm_effect/test_host_table_aliases_omn22755.py"
)


def test_host_table_test_file_has_a_recorded_duration() -> None:
    durations = json.loads((_ROOT / "config" / "test_file_durations.json").read_text())
    assert _TEST_FILE in durations
    assert durations[_TEST_FILE] > 0
