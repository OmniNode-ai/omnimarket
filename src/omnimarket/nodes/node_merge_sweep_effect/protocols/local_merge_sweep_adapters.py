# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The local adapters of the merge-sweep effect ports (OMN-20676)."""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .protocol_merge_sweep_effect import MergeSweepCommandOutcome, MergeSweepPortError

_TIMED_OUT = 124


class LocalMergeSweepLaneLauncher:
    """Runs the remote-lane runner on this host from OMNI_HOME, with no shell."""

    def run(self, argv: Sequence[str], timeout_s: float) -> MergeSweepCommandOutcome:
        try:
            done = subprocess.run(
                list(argv),
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout_s,
                cwd=os.environ.get("OMNI_HOME") or None,
            )
        except subprocess.TimeoutExpired:
            return MergeSweepCommandOutcome(_TIMED_OUT, "", "timed out")
        except OSError as exc:
            raise MergeSweepPortError(f"runner not started: {exc}") from exc
        return MergeSweepCommandOutcome(done.returncode, done.stdout, done.stderr)


class LocalMergeSweepLaneFiles:
    """The lane brief file and the runner's receipt file on this host."""

    def write_text(self, path: str, text: str) -> None:
        try:
            Path(path).write_text(text, encoding="utf-8")
        except OSError as exc:
            raise MergeSweepPortError(f"brief not written: {exc}") from exc

    def read_json(self, path: str) -> dict[str, Any] | None:
        try:
            value = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return value if isinstance(value, dict) else None
