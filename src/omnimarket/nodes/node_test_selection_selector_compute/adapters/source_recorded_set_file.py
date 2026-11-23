# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Real backend: the recorded set read from the replay input and the observed runs file of a checkout.

The observed runs file lists, per merge, the conclusion of the pull request's full-suite run and whether it was
readable (``state`` ``read`` or ``unavailable``). It names no failing test, so a readable run that did not
succeed is a failing run that names none, which the comparison refuses.
"""

from __future__ import annotations

import datetime
import hashlib
import json
from pathlib import Path
from typing import Any

from omnimarket.nodes.node_test_selection_selector_compute.models.model_recorded_set import (
    ModelRecordedSet,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_selection_replay import (
    ModelReplayRequest,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_stage_comparison import (
    ModelRecordedFullRun,
)

_READ = "read"


def _wall_minutes(entry: dict[str, Any]) -> float | None:
    created, updated = entry.get("created_at"), entry.get("updated_at")
    if not (created and updated):
        return None
    start = datetime.datetime.fromisoformat(created)
    end = datetime.datetime.fromisoformat(updated)
    return (end - start).total_seconds() / 60


class SourceRecordedSetFile:
    def __init__(self, repo_root: Path) -> None:
        self._root = repo_root

    def read_recorded_set(
        self, replay_input_path: str, observed_input_path: str
    ) -> ModelRecordedSet | None:
        try:
            replay_bytes = (self._root / replay_input_path).read_bytes()
            observed_bytes = (self._root / observed_input_path).read_bytes()
            replay_input = ModelReplayRequest.model_validate_json(replay_bytes)
            observed = json.loads(observed_bytes)
            runs = tuple(
                ModelRecordedFullRun(
                    sha=entry["sha"],
                    readable=entry["state"] == _READ,
                    conclusion=entry.get("conclusion")
                    if entry["state"] == _READ
                    else None,
                    wall_minutes=_wall_minutes(entry)
                    if entry["state"] == _READ
                    else None,
                )
                for entry in observed["merges"]
            )
        except (OSError, ValueError, KeyError, TypeError):
            return None
        return ModelRecordedSet(
            replay_input=replay_input,
            replay_input_sha256=hashlib.sha256(replay_bytes).hexdigest(),
            runs=runs,
            observed_input_sha256=hashlib.sha256(observed_bytes).hexdigest(),
        )
