# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay-store drill: with the store stopped, the served floor has no red row that reads unowned (OMN-23388, AC3).

The cycle runs the real source reads, the real compute and the real writer over a registry home. The overlay store's fleet
record is the drill: fresh, it leaves a row with no lane unowned (the control that the new reading mutes nothing); a record
whose reads stopped hours ago, or one that is missing, turns every such row to owner unknown in the served document.
"""

from __future__ import annotations

import json
import shutil
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.handlers.emission_sink import InMemoryEmissionSink
from omnimarket.models.floor_17a import ModelFloorSimpleTickRequest
from tests.nodes.node_floor_simple_effect.support import (
    NOW,
    OVERLAYS,
    build_home,
    environ,
    iso,
    record,
)
from tests.nodes.node_floor_simple_effect.test_cycle_exits_floor_simple import handler

pytestmark = pytest.mark.unit

STORE = "overlay_store"
STORE_FILE = ".onex_state/overlay-store/fleet.json"
SERVED = ".claude_scratch/factory-floor/floor-simple.json"


def overlays_with_store(tmp_path: Path) -> Path:
    """The fixture overlays with the overlay store source and the owner-input judgment declared."""
    root = tmp_path / "overlays"
    shutil.copytree(OVERLAYS, root)
    lab = root / "node_floor_simple_effect" / "overlay.yaml"
    text = yaml.safe_load(lab.read_text(encoding="utf-8"))
    text["sources"].append(
        {
            "id": STORE,
            "title": "Overlay store",
            "kind": "overlay_store",
            "max_age_s": 900,
        }
    )
    lab.write_text(yaml.safe_dump(text, sort_keys=False), encoding="utf-8")
    floor = root / "node_floor_simple_compute" / "overlay.yaml"
    body = yaml.safe_load(floor.read_text(encoding="utf-8"))
    body["causes"]["owner_inputs"] = {"owner_map_source": STORE, "lanes_max_age_s": 900}
    floor.write_text(yaml.safe_dump(body, sort_keys=False), encoding="utf-8")
    return root


def fleet_record(age: timedelta) -> dict[str, Any]:
    return {
        "readings": {
            "h051": {
                "read_at": iso(NOW - age),
                "state": "fresh",
                "age_s": int(age.total_seconds()),
            }
        }
    }


def served_after_cycle(tmp_path: Path, store: dict[str, Any] | None) -> dict[str, Any]:
    home = build_home(tmp_path, rec=record(lanes=[]))
    if store is not None:
        path = home / STORE_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(store), encoding="utf-8")
    env = {
        **environ(home),
        "ONEX_SKILL_OVERLAY_ROOTS": str(overlays_with_store(tmp_path)),
    }
    result = handler(InMemoryEmissionSink()).run(
        ModelFloorSimpleTickRequest(home=str(home)), environ=env
    )
    assert result.ok
    served = json.loads((home / SERVED).read_text(encoding="utf-8"))
    assert isinstance(served, dict)
    return served


def red_rows(served: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        c
        for c in served["causes"]
        if c["state"] == "red" and not c["members"] and not c["needs_operator"]
    ]


def test_golden_with_the_store_fresh_a_red_row_with_no_lane_reads_unowned(
    tmp_path: Path,
) -> None:
    served = served_after_cycle(tmp_path, fleet_record(timedelta(minutes=2)))
    rows = red_rows(served)
    assert rows
    assert {c["owner_state"] for c in rows} == {"unowned"}
    assert served["owner_inputs"]["unknown"] is False
    assert served["red_rule"]["owner_unknown_red"] == []


def test_error_with_the_store_stopped_no_red_row_reads_unowned(tmp_path: Path) -> None:
    served = served_after_cycle(tmp_path, fleet_record(timedelta(hours=5, minutes=19)))
    rows = red_rows(served)
    assert rows
    assert {c["owner_state"] for c in rows} == {"unknown"}
    assert not [c for c in served["causes"] if c["owner_state"] == "unowned"]
    assert served["red_rule"]["unowned_red"] == []
    assert served["red_rule"]["owner_unknown_red"]
    assert "owner map read 319 min ago" in served["owner_inputs"]["reason"]


def test_error_with_the_store_record_missing_no_red_row_reads_unowned(
    tmp_path: Path,
) -> None:
    served = served_after_cycle(tmp_path, None)
    assert red_rows(served)
    assert not [c for c in served["causes"] if c["owner_state"] == "unowned"]
    assert "owner map unreadable" in served["owner_inputs"]["reason"]
