# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21733: the runner-fleet projection saves the newest observation where the factory floor reads it."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_factory_floor_effect.source_loader import (
    load_runner_fleet_strict,
    runner_fleet_cause,
)
from omnimarket.nodes.node_projection_runner_fleet.handlers import fleet_state_file
from omnimarket.nodes.node_projection_runner_fleet.handlers.fleet_state_file import (
    SAVED,
    SUPERSEDED,
    save_observation,
    state_file_path,
)
from omnimarket.nodes.node_projection_runner_fleet.models import (
    ModelRunnerFleetObservationWire,
)

pytestmark = pytest.mark.unit

_T0 = datetime(2026, 11, 1, 23, 0, 0, tzinfo=UTC)
_CONTRACT = Path(fleet_state_file.__file__).resolve().parents[1] / "contract.yaml"


def _observation(
    *, observed_at: datetime = _T0, statuses: tuple[str, ...] = ("online", "busy")
) -> ModelRunnerFleetObservationWire:
    runners: list[dict[str, Any]] = [
        {
            "runner_name": f"runner-{n}",
            "label_class": "omnibase-ci",
            "host": f"host-05{n}",
            "status": status,
            "observed_at": observed_at.isoformat(),
        }
        for n, status in enumerate(statuses)
    ]
    return ModelRunnerFleetObservationWire.model_validate(
        {
            "schema_version": "1.0.0",
            "event_type": "runner-fleet-observation",
            "host": "omni-201",
            "observed_at": observed_at.isoformat(),
            "runners": runners,
        }
    )


def test_the_contract_declares_where_the_observation_is_saved_and_the_path_follows_the_state_directory(
    tmp_path: Path,
) -> None:
    contract = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    path = state_file_path(contract, {"ONEX_STATE_DIR": str(tmp_path)})
    assert path == tmp_path / "runner-fleet" / "runner-fleet.json"
    assert state_file_path(contract, {}) is None


def test_a_saved_observation_is_the_file_the_floor_reads(tmp_path: Path) -> None:
    path = tmp_path / "runner-fleet" / "runner-fleet.json"
    assert "does not exist" in runner_fleet_cause(path)
    assert save_observation(_observation(), path) == SAVED
    fleet = load_runner_fleet_strict(path)
    assert fleet.observed_at == _T0
    assert [(r.host_label, r.status) for r in fleet.runners] == [
        ("host-050", "online"),
        ("host-051", "busy"),
    ]
    assert runner_fleet_cause(path) == ""


def test_an_older_or_equal_observation_never_replaces_a_newer_file(
    tmp_path: Path,
) -> None:
    path = tmp_path / "runner-fleet.json"
    newer = _observation(observed_at=_T0 + timedelta(minutes=3), statuses=("offline",))
    assert save_observation(newer, path) == SAVED
    assert save_observation(_observation(), path) == SUPERSEDED
    assert save_observation(newer, path) == SUPERSEDED
    assert json.loads(path.read_text(encoding="utf-8"))["runners"] == [
        {"host": "host-050", "status": "offline"}
    ]
    assert (
        save_observation(_observation(observed_at=_T0 + timedelta(minutes=6)), path)
        == SAVED
    )


def test_an_unreadable_file_is_replaced_not_trusted(tmp_path: Path) -> None:
    path = tmp_path / "runner-fleet.json"
    path.write_text("{not json", encoding="utf-8")
    assert save_observation(_observation(), path) == SAVED
    assert runner_fleet_cause(path) == ""
    assert [p.name for p in tmp_path.iterdir()] == ["runner-fleet.json"]
