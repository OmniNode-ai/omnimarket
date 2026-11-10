# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay selects the watcher source: injected records by default, the watcher's state file for Redpanda (OMN-23409)."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from omnimarket.nodes.node_pr_landing_snapshot_effect.handlers.handler_pr_landing_snapshot import (
    HandlerPrLandingSnapshot,
)
from omnimarket.nodes.node_pr_landing_snapshot_effect.handlers.snapshot_sources import (
    InMemoryWatcherSource,
    WatcherStateFileSource,
    build_emission,
    build_watcher_source,
    load_snapshot_overlay,
    watcher_key,
    watcher_pr_read,
)
from omnimarket.nodes.node_pr_landing_snapshot_effect.models.model_landing_snapshot import (
    EnumSnapshotFailureReason,
    EnumSnapshotStatus,
    ModelLandingSnapshotRequest,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.models.model_tick_orchestrator import (
    ModelTickSnapshot,
)

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
TICK = (
    REPO / "tests" / "fixtures" / "pr_landing_tick_replay" / "tick_725_omnimarket.json"
)
INMEMORY = {
    "backend": "inmemory",
    "failed_conclusions": ["failure", "cancelled", "timed_out"],
    "floor_piece": 9,
}
REDPANDA = {
    **INMEMORY,
    "backend": "redpanda",
    "bootstrap_servers": "broker.example:9092",
    "emission_journal": ".onex_state/example/emissions.jsonl",
}


def _tick() -> ModelTickSnapshot:
    return ModelTickSnapshot.model_validate_json(TICK.read_text(encoding="utf-8"))


def _state_file(tmp_path: Path) -> Path:
    """A watcher state file in the watcher's own shape: prs keyed repo#n."""
    tick = _tick()
    prs = {
        watcher_key(pr.pr): {
            "facts": {"node_id": f"PR_file_{i}", "head_sha": pr.head_sha},
            "ci": {
                "sha": pr.head_sha,
                "read_at": "2026-11-10T01:00:00Z",
                "red_facts": [
                    [
                        "Tests (Split 13/20)",
                        "failure",
                        str(100 + i),
                        "2026-11-10T01:00:00Z",
                    ],
                    ["junk row", "failure", "not-a-run", "2026-11-10T01:00:00Z"],
                ],
            },
        }
        for i, pr in enumerate(tick.facts.prs)
    }
    path = tmp_path / "pr-watcher-state.json"
    path.write_text(json.dumps({"schema": 1, "prs": prs}), encoding="utf-8")
    return path


def test_inmemory_overlay_takes_the_injected_records() -> None:
    overlay = load_snapshot_overlay(explicit=INMEMORY)
    injected = InMemoryWatcherSource({})
    assert build_watcher_source(overlay, inmemory=injected) is injected
    _, backend = build_emission(overlay)
    assert backend == "in_memory"


def test_inmemory_overlay_without_records_is_refused() -> None:
    with pytest.raises(ValueError, match="injected"):
        build_watcher_source(load_snapshot_overlay(explicit=INMEMORY))


def test_redpanda_overlay_reads_the_watcher_state_file(tmp_path: Path) -> None:
    path = _state_file(tmp_path)
    overlay = load_snapshot_overlay(
        explicit={**REDPANDA, "watcher_state_path": str(path)}
    )
    source = build_watcher_source(overlay)
    assert isinstance(source, WatcherStateFileSource)
    tick = _tick()
    result = HandlerPrLandingSnapshot(
        failed_conclusions=overlay.failed_conclusions, watcher=source
    ).handle(
        ModelLandingSnapshotRequest(
            correlation_id=uuid4(), facts=tick.facts, decision=tick.decision
        )
    )
    assert result.status is EnumSnapshotStatus.RECORDED
    assert result.snapshot is not None
    assert [p.failed_run_ids for p in result.snapshot.facts.prs] == [
        (100 + i,) for i in range(len(tick.facts.prs))
    ]
    assert result.node_ids_filled == len(tick.facts.prs)
    assert result.unread_prs == ()


def test_a_missing_watcher_state_file_fails_the_tick_unrecorded(tmp_path: Path) -> None:
    overlay = load_snapshot_overlay(
        explicit={**REDPANDA, "watcher_state_path": str(tmp_path / "absent.json")}
    )
    tick = _tick()
    result = HandlerPrLandingSnapshot(
        failed_conclusions=overlay.failed_conclusions,
        watcher=build_watcher_source(overlay),
    ).handle(
        ModelLandingSnapshotRequest(
            correlation_id=uuid4(), facts=tick.facts, decision=tick.decision
        )
    )
    assert result.failure_reason is EnumSnapshotFailureReason.WATCHER_UNREADABLE
    assert result.snapshot is None


@pytest.mark.parametrize(
    "missing", ["watcher_state_path", "bootstrap_servers", "emission_journal"]
)
def test_redpanda_overlay_missing_a_field_is_refused(missing: str) -> None:
    doc = {**REDPANDA, "watcher_state_path": "state.json"}
    doc.pop(missing)
    with pytest.raises(ValueError, match=missing):
        load_snapshot_overlay(explicit=doc)


def test_an_overlay_with_no_failed_conclusions_is_refused() -> None:
    with pytest.raises(ValueError, match="failed_conclusions"):
        load_snapshot_overlay(explicit={**INMEMORY, "failed_conclusions": []})


def test_the_watcher_read_takes_the_run_id_column_and_skips_a_bad_row() -> None:
    read = watcher_pr_read(
        {
            "facts": {"node_id": "PR_x"},
            "ci": {
                "sha": "a" * 40,
                "red_facts": [
                    [
                        "Tests (Split 13/20)",
                        "failure",
                        "42734162890",
                        "2026-11-10T01:32:04Z",
                    ],
                    ["short row", "failure"],
                    ["zero", "failure", "0", "2026-11-10T01:32:04Z"],
                ],
            },
        }
    )
    assert read.node_id == "PR_x"
    assert [r.run_id for r in read.red_runs] == [42734162890]
