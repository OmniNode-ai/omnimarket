# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A stoppage whose alarm wrote a resolved STATUS row after its last FRICTION row ends on that evidence (OMN-22868).

On 2026-11-09 the throughput-results alarm raised at 02:04:19Z and wrote ``resolved=throughput-results`` at 02:15:33Z,
yet the floor drew the stoppage red for the overlay's whole window (3 hours after the last FRICTION row), because the
friction report carried only ``last_seen``. The report now carries ``resolved_at`` and the floor reads it. The chain
below runs the two real rows through the rollup, the report loader, the record reader and the floor; each control
changes one input (the resolved row removed, or a newer FRICTION row after it) and the stoppage is red again.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from omnimarket.models.floor_17a import (
    ModelFloorLane,
    ModelFloorSimple,
    ModelFloorSimpleInput,
    ModelFloorSimpleOverlay,
)
from omnimarket.models.friction_rollup import (
    ModelFrictionRollupRequest,
    ModelFrictionRollupSource,
)
from omnimarket.nodes.node_factory_floor_compute.models.model_factory_floor_output import (
    ModelFloorStoppageOut,
)
from omnimarket.nodes.node_factory_floor_effect.source_loader import load_stoppages
from omnimarket.nodes.node_floor_simple_compute.handlers.handler_floor_simple import (
    HandlerFloorSimple,
)
from omnimarket.nodes.node_floor_simple_effect.handlers.sources import (
    record_stoppages,
)
from omnimarket.nodes.node_friction_rollup_compute.handlers.handler_friction_rollup import (
    HandlerFrictionRollup,
)
from tests.nodes.node_floor_simple_effect.support import overlay_text

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 9, 2, 56, 31, tzinfo=UTC)
FRICTION = (
    "2026-11-09T02:04:19Z | FRICTION | lane=silent-failure-alarm | ticket=OMN-19998 | class=throughput-results | "
    "short=1 | cost=est ~60 minutes of factory output short of its bound | 3 merges in the last hour with 243 open "
    "non-draft PRs (bound 5)"
)
RESOLVED = (
    "2026-11-09T02:15:33Z | STATUS | lane=silent-failure-alarm | ticket=OMN-19998 | "
    "episode=al-20261109T0204Z-throughput-results | resolved=throughput-results | open_minutes=11 | "
    "throughput-results alarm resolved after 11 minutes open"
)
LATER = FRICTION.replace("2026-11-09T02:04:19Z", "2026-11-09T02:30:00Z")


def rollup(*rows: str) -> dict[str, object]:
    request = ModelFrictionRollupRequest(
        sources=[
            ModelFrictionRollupSource(
                display="ledger-fixture.md",
                kind="markdown",
                text="\n".join(rows) + "\n",
            )
        ],
        since=(NOW - timedelta(hours=12)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        until=NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
        generated_at=NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
        bucket_hours=2,
        umbrella_tickets=["OMN-17427"],
        aliases=[],
    )
    return HandlerFrictionRollup().handle(request).report


def floor_cause(
    report: dict[str, object], tmp_path: Path, lanes=()
) -> dict[str, object]:
    """The stoppage cause as the floor draws it, from the rollup report through the loader and the record reader."""
    report_file = tmp_path / "friction.json"
    report_file.write_text(json.dumps(report), encoding="utf-8")
    stoppages = [
        ModelFloorStoppageOut.model_validate(s, from_attributes=True).model_dump()
        for s in load_stoppages(report_file)
    ]
    doc = floor_doc(record_stoppages({"floor": {"stoppages": stoppages}}), lanes)
    cause = next(c for c in doc.causes if c.id == "stoppage:throughput-results")
    assert not doc.red_rule.violations
    return cause.model_dump()


def floor_doc(stoppages, lanes) -> ModelFloorSimple:
    return HandlerFloorSimple().handle(
        ModelFloorSimpleInput(
            now=NOW,
            overlay=ModelFloorSimpleOverlay.model_validate(
                overlay_text("node_floor_simple_compute")
            ),
            open_prs=10,
            open_prs_before=12,
            merges_in_window=4,
            watcher_read_at=NOW - timedelta(minutes=1),
            slots_working=5,
            slots_total=20,
            record_read_at=NOW - timedelta(minutes=1),
            stoppages=stoppages,
            lanes=lanes,
            lab=None,
            receipts=None,
            go_given_through=0,
        )
    )


def test_golden_a_resolved_row_after_the_last_friction_row_ends_the_stoppage(
    tmp_path: Path,
) -> None:
    report = rollup(FRICTION, RESOLVED)
    [item] = [c for c in report["classes"] if c["class"] == "throughput-results"]
    assert item["last_seen"] == "2026-11-09T02:04:19Z"
    assert item["resolved_at"] == "2026-11-09T02:15:33Z"
    cause = floor_cause(report, tmp_path)
    assert cause["still_occurring"] is False
    assert cause["state"] == "quiet"
    assert cause["resolved_at"] == datetime(2026, 11, 9, 2, 15, 33, tzinfo=UTC)


def test_error_without_the_resolved_row_the_stoppage_is_red_in_the_window(
    tmp_path: Path,
) -> None:
    # The failing control: the same ledger with the STATUS row taken away is what the floor read on 2026-11-09.
    report = rollup(FRICTION)
    [item] = report["classes"]
    assert "resolved_at" not in item
    cause = floor_cause(report, tmp_path)
    assert cause["still_occurring"] is True
    assert cause["state"] == "red"


def test_error_a_friction_row_after_the_resolved_row_makes_it_occurring_again(
    tmp_path: Path,
) -> None:
    report = rollup(FRICTION, RESOLVED, LATER)
    [item] = [c for c in report["classes"] if c["class"] == "throughput-results"]
    assert item["last_seen"] == "2026-11-09T02:30:00Z"
    assert item["resolved_at"] == "2026-11-09T02:15:33Z"
    cause = floor_cause(report, tmp_path)
    assert cause["still_occurring"] is True
    assert cause["state"] == "red"


def test_error_a_resolved_row_for_another_class_ends_nothing(tmp_path: Path) -> None:
    other = RESOLVED.replace("resolved=throughput-results", "resolved=quiet-host")
    report = rollup(FRICTION, other)
    [item] = report["classes"]
    assert "resolved_at" not in item
    assert floor_cause(report, tmp_path)["state"] == "red"


def test_golden_a_resolved_cell_lists_several_classes_and_none_names_no_class() -> None:
    both = RESOLVED.replace(
        "resolved=throughput-results", "resolved=quiet-host,throughput-results"
    )
    none = RESOLVED.replace("resolved=throughput-results", "resolved=none")
    [item] = rollup(FRICTION, both)["classes"]
    assert item["resolved_at"] == "2026-11-09T02:15:33Z"
    [item] = rollup(FRICTION, none)["classes"]
    assert "resolved_at" not in item


def test_golden_a_live_lane_on_a_resolved_stoppage_draws_amber_not_red(
    tmp_path: Path,
) -> None:
    lane = ModelFloorLane(
        lane="here", liveness="alive", owns=("stoppage:throughput-results",)
    )
    cause = floor_cause(rollup(FRICTION, RESOLVED), tmp_path, (lane,))
    assert cause["still_occurring"] is False
    assert cause["state"] == "amber"
