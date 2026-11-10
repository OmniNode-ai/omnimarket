# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The runner groups row splits offline runners by the host table and the plan; a real problem stays red and named."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from omnimarket.models.floor_17a import ModelLabStateOverlay
from omnimarket.nodes.node_floor_simple_effect.handlers.sources import (
    read_runner_groups,
)
from tests.nodes.node_floor_simple_effect.support import overlay_text

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 6, 6, 0, tzinfo=UTC)
POOL = "pool-host"


def _overlay() -> ModelLabStateOverlay:
    return ModelLabStateOverlay.model_validate(overlay_text("node_floor_simple_effect"))


def _record(h202: str = "repair") -> dict[str, Any]:
    machine = {
        "host": "h202",
        "state": h202,
        "note": "h202 is down for repair (operator)",
    }
    return {"floor": {"machines": [machine, {"host": "h201", "state": "read"}]}}


def _row(name: str, status: str) -> dict[str, Any]:
    return {
        "host": POOL,
        "status": status,
        "runner_name": name,
        "label_class": "ci",
        "labels": ["self-hosted"],
    }


def _fleet(tmp_path: Path, rows: list[dict[str, Any]]) -> Path:
    path = tmp_path / "fleet.json"
    path.write_text(
        json.dumps({"observed_at": NOW.isoformat(), "runners": rows}), encoding="utf-8"
    )
    return path


def _by_name(read_subjects: Any) -> dict[str, Any]:
    return {s.name: s for s in read_subjects}


def test_golden_runners_on_a_host_in_repair_read_as_one_line_and_the_group_is_ok(
    tmp_path: Path,
) -> None:
    rows = [_row(f"spare-{n}", "offline") for n in range(20)]
    rows += [_row(f"omninode-runner-{n}", "online") for n in range(1, 4)]
    read = read_runner_groups(_fleet(tmp_path, rows), _overlay(), _record())
    subjects = _by_name(read.subjects)
    assert subjects[POOL].ok
    assert "20 of the offline are parked on purpose, 0 are not" in subjects[POOL].note
    assert subjects["h202 runners"].ok
    assert subjects["h202 runners"].note.startswith("20 runners offline, parked")
    assert [s.name for s in read.subjects if not s.ok] == []


def test_error_the_failing_control_without_the_repair_declaration_the_same_rows_stay_red(
    tmp_path: Path,
) -> None:
    rows = [_row(f"spare-{n}", "offline") for n in range(20)]
    read = read_runner_groups(_fleet(tmp_path, rows), _overlay(), _record(h202="read"))
    subjects = _by_name(read.subjects)
    assert not subjects[POOL].ok
    assert (
        sum(1 for s in read.subjects if not s.ok and s.name.startswith("spare-")) == 20
    )
    assert "h202 runners" not in subjects


def test_error_a_runner_meant_to_run_that_is_offline_stays_red_and_says_what_restores_it(
    tmp_path: Path,
) -> None:
    rows = [_row("omninode-runner-1", "online"), _row("omninode-runner-18", "offline")]
    read = read_runner_groups(_fleet(tmp_path, rows), _overlay(), _record())
    bad = _by_name(s for s in read.subjects if not s.ok)
    assert "omninode-runner-18" in bad
    assert "restored by: recreate the container" in bad["omninode-runner-18"].note
    assert not _by_name(read.subjects)[POOL].ok


def test_golden_a_runner_the_plan_leaves_stopped_is_parked_by_the_plan(
    tmp_path: Path,
) -> None:
    rows = [_row("omninode-prod-deploy-runner-1", "offline")]
    read = read_runner_groups(_fleet(tmp_path, rows), _overlay(), _record())
    assert all(s.ok for s in read.subjects)
    assert any("left stopped by the plan" in s.note for s in read.subjects)


def test_error_an_offline_runner_no_row_names_stays_red(tmp_path: Path) -> None:
    read = read_runner_groups(
        _fleet(tmp_path, [_row("mystery-1", "offline")]), _overlay(), _record()
    )
    bad = _by_name(s for s in read.subjects if not s.ok)
    assert "no host table or plan row names it" in bad["mystery-1"].note


def test_error_an_older_saved_file_without_names_cannot_be_split_and_stays_red(
    tmp_path: Path,
) -> None:
    rows = [{"host": POOL, "status": "offline"}] * 3
    read = read_runner_groups(_fleet(tmp_path, rows), _overlay(), _record())
    bad = _by_name(s for s in read.subjects if not s.ok)
    assert "unnamed runners" in bad
    assert "carry no name" in bad["unnamed runners"].note
