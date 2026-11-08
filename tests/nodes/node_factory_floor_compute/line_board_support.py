# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Runs the factory floor effect over one frozen line board fixture (OMN-22367, lean line board plan T2).

A fixture directory mirrors the sources the effect reads: ``home/`` (the registry home with the watcher state, the
plans-today output, the required contexts), ``kb/`` (the plans clone), ``ledger-rows.txt`` (the ledger reader's
output) and ``fixture.yaml`` (the observation time, how every part was derived, the expected stops). A source the
fixture marks absent is a ``<name>.ABSENT`` file holding the reason; the effect sees no file there.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.models.model_merged_running_count import (
    ModelMrcEvidence,
    ModelMrcEvidenceRead,
)
from omnimarket.nodes.node_factory_floor_effect.handlers.handler_factory_floor_effect import (
    HandlerFactoryFloorEffect,
)
from omnimarket.nodes.node_factory_floor_effect.models.model_factory_floor_run import (
    ModelFactoryFloorRunRequest,
)

FIXTURES = Path(__file__).parent / "fixtures" / "line_board_2026_11_03"
CASES = (
    "s1_review_not_wired",
    "s2_lab_fill_did_nothing",
    "s3_person_blocker_unverified",
    "s4_ruled_not_started",
    "s5_held_with_real_red",
    "s6_release_at_old_head",
    "s7_all_sources_unread",
)
VARIANTS = ("observed", "repaired")
OUTPUT_RELPATH = ".claude_scratch/factory-floor/factory-floor.json"
# Variables the effect reads when set: dropped so the temporary home is the only place it looks.
ENV_DROPPED = (
    "ONEX_REMOTE_LANE_DIR",
    "LANDING_PLACEMENT_DIR",
    "ONEX_LAB_RUN_HOSTS",
    "ONEX_FACTORY_FLOOR_PHOENIX_URL",
    "ONEX_FACTORY_FLOOR_OVERLAY",
    "CLAUDE_PLUGIN_ROOT",
)


def variant_dir(case: str, variant: str) -> Path:
    return FIXTURES / case / ("repaired" if variant == "repaired" else "")


def header(case: str, variant: str) -> dict[str, Any]:
    loaded = yaml.safe_load(
        (variant_dir(case, variant) / "fixture.yaml").read_text(encoding="utf-8")
    )
    assert isinstance(loaded, dict)
    return loaded


def overlay_keys(path: Path) -> dict[str, Any]:
    """The line board declarations of the shared deployment data file, in the form the floor overlay takes them.

    The data file keeps each process's run record and did-nothing match as nested data; the overlay holds them as the
    ``key=value`` conditions the compute matches a ledger row against (``;`` between conditions).
    """
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    processes = []
    for process in data["processes"]:
        record = process.get("run_record") or {}
        nothing = process.get("did_nothing") or {}
        conditions = []
        if nothing.get("class"):
            conditions.append(f"class={nothing['class']}")
        if nothing.get("text_contains"):
            conditions.append(f"tier-reasons~{nothing['text_contains']}")
        conditions.extend(
            f"{cell}>{floor}"
            for cell, floor in (nothing.get("cell_greater_than") or {}).items()
        )
        processes.append(
            {
                "name": process["name"],
                "actor": process.get("actor") or "",
                "run_kind": record.get("kind", ""),
                "run_match": f"actor={record['actor']}" if record else "",
                "did_nothing_match": ";".join(conditions),
                "interval_min": process.get("interval_minutes") or 0,
                "outcomes_covered": process.get("outcomes_covered") or [],
            }
        )
    return {
        "processes": processes,
        "roles": [role["role"] for role in data["roles"]],
        "release_required_repos": data["release_required_repos"],
        "held_gates": data["held_gates"],
    }


class _FixtureRunningReader:
    """The bound merged-running reader of a replay: the running evidence the fixture recorded, none when it has none."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def read(self, now: datetime, *, hosts_table: Path | None = None) -> Any:
        raise OSError("the replay reads evidence, not a count")

    def read_evidence(
        self, now: datetime, *, hosts_table: Path | None = None
    ) -> ModelMrcEvidenceRead:
        if not self._path.exists():
            return ModelMrcEvidenceRead(unread_cause="no running evidence was recorded")
        rows = json.loads(self._path.read_text(encoding="utf-8"))
        return ModelMrcEvidenceRead(
            evidence=tuple(ModelMrcEvidence.model_validate(row) for row in rows)
        )


def run_board(
    case: str, variant: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Any]:
    """The floor record the effect writes for the fixture, read back as JSON."""
    source = variant_dir(case, variant)
    head = header(case, variant)
    keep = shutil.ignore_patterns("*.ABSENT")
    home = tmp_path / "home"
    kb = tmp_path / "kb"
    for name, target in (("home", home), ("kb", kb)):
        if (source / name).is_dir():
            shutil.copytree(source / name, target, ignore=keep)
        else:
            target.mkdir()
    # The directory is stored without its leading dot: the repository ignores .claude_scratch.
    if (home / "claude_scratch").is_dir():
        (home / "claude_scratch").rename(home / ".claude_scratch")
    # A fixture whose plans clone is absent holds only the marker file: the clone has no plans directory.
    if (source / "kb" / "beta" / "plans" / "NONE.md.ABSENT").exists():
        shutil.rmtree(kb / "beta" / "plans", ignore_errors=True)
    else:
        (kb / "beta" / "plans").mkdir(parents=True, exist_ok=True)
    root = tmp_path / "overlays" / "node_factory_floor_effect"
    root.mkdir(parents=True)
    contains = list(head["plan_path_contains"]) or ["no-plan-is-declared-by-this-case"]
    (root / "overlay.yaml").write_text(
        yaml.safe_dump(
            {
                "plan_path_contains": contains,
                **overlay_keys(source / head["overlay_file"]),
            }
        ),
        encoding="utf-8",
    )
    shutil.copy(source / head["overlay_file"], root / "line_board.yaml")
    rows = source / "ledger-rows.txt"
    for name in ENV_DROPPED:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path / "overlays"))
    HandlerFactoryFloorEffect(
        _FixtureRunningReader(home / ".claude_scratch" / "running-evidence.json")
    ).handle(
        ModelFactoryFloorRunRequest(
            now=datetime.fromisoformat(head["observed_at"].replace("Z", "+00:00")),
            home=str(home),
            kb=str(kb),
            ledger_rows_file=str(rows) if rows.exists() else str(tmp_path / "none"),
        )
    )
    record = json.loads((home / OUTPUT_RELPATH).read_text(encoding="utf-8"))
    assert isinstance(record, dict)
    return record


def stops_by_line(record: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    """(line id, stop kind) to the stop. A record with no ``stops`` field yields nothing."""
    found: dict[tuple[str, str], dict[str, Any]] = {}
    for line in record.get("lines", []):
        for stop in line.get("stops", []) or []:
            found[(line["line_id"], stop["kind"])] = stop
    return found


def evidence_refs(stop: dict[str, Any]) -> set[str]:
    return {str(item.get("ref", "")) for item in stop.get("evidence", [])}
