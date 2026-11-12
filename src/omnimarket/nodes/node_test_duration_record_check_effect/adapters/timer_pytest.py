# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Time one test file by running the policy's measure command and reading its durations file."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

from ..models.model_test_duration_record_check import ModelTestDurationRecordPolicy


class TimerPytest:
    """Runs the policy's ``measure_command`` and sums the durations it writes."""

    def __init__(self, policy: ModelTestDurationRecordPolicy) -> None:
        self._policy = policy

    def measure(self, repo_root: str, test_file: str) -> float | None:
        """Return the summed seconds, or ``None`` when the command fails or the file is unreadable."""
        with tempfile.TemporaryDirectory() as tmp:
            durations_path = Path(tmp) / "durations.json"
            argv = [
                sys.executable
                if element == "python"
                else element.replace("{file}", test_file).replace(
                    "{durations_path}", str(durations_path)
                )
                for element in self._policy.measure_command
            ]
            completed = subprocess.run(
                argv, cwd=repo_root, check=False, capture_output=True, text=True
            )
            if completed.returncode != 0:
                return None
            try:
                data = json.loads(durations_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return None
            if not isinstance(data, dict) or not data:
                return None
            return float(sum(data.values()))
