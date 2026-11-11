# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-22605 AC1: the saved fleet observation carries each runner's name, label class and labels.

The floor reads that file to tell offline runners apart. A file saved before the producer added the names reads as
unsplit: the floor cannot tell its offline runners apart, and the row stays red.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_projection_runner_fleet.handlers.fleet_state_file import (
    SAVED,
    save_observation,
)
from omnimarket.nodes.node_projection_runner_fleet.models import (
    ModelRunnerFleetObservationWire,
)
from tests.nodes.node_floor_simple_effect.support import NOW, build_home, iso
from tests.nodes.node_floor_simple_effect.test_golden_chain_floor_simple import (
    run,
    written,
)

pytestmark = pytest.mark.unit

_LABELS = ["self-hosted", "arch-amd64", "host-051"]


def _observation(status: str) -> ModelRunnerFleetObservationWire:
    runner: dict[str, Any] = {
        "runner_name": "omninode-runner-7",
        "label_class": "omnibase-ci",
        "labels": _LABELS,
        "host": "host-051",
        "status": status,
        "observed_at": iso(NOW),
    }
    return ModelRunnerFleetObservationWire.model_validate(
        {
            "schema_version": "1.0.0",
            "event_type": "runner-fleet-observation",
            "host": "omni-201",
            "observed_at": iso(NOW),
            "runners": [runner],
        }
    )


def _runner_line(home: Path) -> Any:
    return next(x for x in written(home).lab_state if x.id == "runner_groups")


def test_golden_a_saved_observation_carries_name_label_class_and_labels(
    tmp_path: Path,
) -> None:
    path = tmp_path / "runner-fleet.json"
    assert save_observation(_observation("offline"), path) == SAVED
    (saved,) = json.loads(path.read_text(encoding="utf-8"))["runners"]
    assert saved["runner_name"] == "omninode-runner-7"
    assert saved["label_class"] == "omnibase-ci"
    assert saved["labels"] == _LABELS


def test_golden_the_floor_names_the_offline_runner_from_the_saved_file(
    tmp_path: Path,
) -> None:
    home = build_home(tmp_path)
    saved = home / ".onex_state/runner-fleet/runner-fleet.json"
    assert save_observation(_observation("offline"), saved) == SAVED
    run(home)
    line = _runner_line(home)
    assert line.state == "NOT WORKING"
    assert "omninode-runner-7" in line.text


def test_error_an_older_file_without_the_names_reads_unsplit_and_stays_red(
    tmp_path: Path,
) -> None:
    path = tmp_path / "runner-fleet.json"
    save_observation(_observation("offline"), path)
    document = json.loads(path.read_text(encoding="utf-8"))
    for row in document["runners"]:
        for key in ("runner_name", "label_class", "labels"):
            del row[key]
    home = build_home(tmp_path / "older", fleet_doc=document)
    run(home)
    line = _runner_line(home)
    assert line.state == "NOT WORKING"
    assert "unnamed runners" in line.text
