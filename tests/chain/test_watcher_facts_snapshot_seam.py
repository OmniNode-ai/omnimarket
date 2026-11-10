# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam: the watcher facts source -> the landing snapshot step (OMN-23507).

The watcher facts node writes the landing facts file for the repositories its overlay names; the
snapshot step's file facts source reads that file and its write-facts source reads the same PR
watcher state. The seam holds when the snapshot step records the tick the facts node wrote, with
every open pull request of the named repositories and their node ids filled.

Named breaks (each must turn this file red):
- a field of the written facts renamed (the facts node's side changes shape): the snapshot step's
  facts source refuses the file;
- a pull request key written under another owner (the key the two sides share changes shape):
  the snapshot step fills no node id for it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from omnimarket.nodes.node_pr_landing_snapshot_effect.handlers.handler_pr_landing_snapshot import (
    HandlerPrLandingSnapshot,
)
from omnimarket.nodes.node_pr_landing_snapshot_effect.handlers.snapshot_errors import (
    SnapshotSourceError,
)
from omnimarket.nodes.node_pr_landing_snapshot_effect.handlers.snapshot_ports import (
    InMemoryLandingSnapshotPublisher,
    InMemoryLandingTickRecorder,
)
from omnimarket.nodes.node_pr_landing_snapshot_effect.handlers.snapshot_sources import (
    LandingFactsFileSource,
    WatcherStateWriteFactsSource,
)
from omnimarket.nodes.node_pr_landing_snapshot_effect.models.model_landing_snapshot import (
    EnumSnapshotStatus,
    ModelLandingSnapshotRequest,
)
from omnimarket.nodes.node_pr_landing_snapshot_effect.models.model_snapshot_overlay import (
    ModelSnapshotOverlay,
)
from omnimarket.nodes.node_pr_landing_watcher_facts_effect.handlers.handler_pr_landing_watcher_facts import (
    HandlerPrLandingWatcherFacts,
)
from omnimarket.nodes.node_pr_landing_watcher_facts_effect.handlers.watcher_facts_ports import (
    InMemoryWatcherFactsPublisher,
)
from omnimarket.nodes.node_pr_landing_watcher_facts_effect.handlers.watcher_facts_sources import (
    AtomicLandingFactsFile,
    WatcherStateFile,
)
from omnimarket.nodes.node_pr_landing_watcher_facts_effect.models.model_watcher_facts import (
    EnumWatcherFactsStatus,
    ModelWatcherFactsRequest,
)
from omnimarket.nodes.node_pr_landing_watcher_facts_effect.models.model_watcher_facts_overlay import (
    EnumWatcherFactsBackend,
    ModelWatcherFactsOverlay,
)

pytestmark = pytest.mark.unit

OWNER = "OmniNode-ai"
NOW = datetime(2026, 11, 10, 9, 0, 0, tzinfo=UTC)
STAMP = (NOW - timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
FIXED_ID = UUID("00000000-0000-4000-8000-000000000007")
HEAD_A = "a" * 40
HEAD_B = "b" * 40
HEAD_C = "c" * 40


def _entry(
    repo: str, number: int, head: str, *, state: str, verdict: str
) -> dict[str, object]:
    red = verdict == "RED"
    return {
        "facts": {
            "repo": repo,
            "number": number,
            "state": state,
            "draft": False,
            "head_sha": head,
            "node_id": f"PR_node_{repo}_{number}",
            "created_at": "2026-11-09T22:25:22Z",
        },
        "ci": {
            "sha": head,
            "verdict": verdict,
            "red": ["Tests (Split 1/2)"] if red else [],
            "cancelled": [],
            "red_facts": [["Tests (Split 1/2)", "failure", "4242", STAMP]]
            if red
            else [],
        },
        "review": {"merge_state": "BLOCKED" if red else "CLEAN"},
        "queued": False,
    }


def _watcher_state() -> dict[str, object]:
    return {
        "last_tick": STAMP,
        "prs": {
            "omnimemory#713": _entry(
                "omnimemory", 713, HEAD_A, state="OPEN", verdict="GREEN"
            ),
            "omnimemory#725": _entry(
                "omnimemory", 725, HEAD_B, state="OPEN", verdict="RED"
            ),
            "omnimemory#737": _entry(
                "omnimemory", 737, HEAD_C, state="MERGED", verdict="GREEN"
            ),
            "omnimarket#5711": _entry(
                "omnimarket", 5711, HEAD_C, state="OPEN", verdict="GREEN"
            ),
        },
    }


def _facts_overlay(owner: str = OWNER) -> ModelWatcherFactsOverlay:
    return ModelWatcherFactsOverlay(
        backend=EnumWatcherFactsBackend.INMEMORY,
        repositories=("omnimemory",),
        owner=owner,
        unclassified_red_class="product",
        written_topic="onex.evt.omnimarket.pr-landing-watcher-facts-written.v1",
        failed_topic="onex.evt.omnimarket.pr-landing-watcher-facts-failed.v1",
        floor_piece=9,
        watcher_max_age_seconds=900,
    )


def _snapshot_overlay() -> ModelSnapshotOverlay:
    return ModelSnapshotOverlay(
        backend="inmemory",
        record_topic="onex.evt.omnimarket.pr-landing-tick-facts-recorded.v1",
        failed_topic="onex.evt.omnimarket.pr-landing-snapshot-failed.v1",
        floor_piece=9,
    )


def _write_facts(tmp_path: Path, owner: str = OWNER) -> tuple[Path, Path, int]:
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps(_watcher_state()), encoding="utf-8")
    facts_path = tmp_path / "last-facts.json"
    result = HandlerPrLandingWatcherFacts(
        overlay=_facts_overlay(owner),
        state_source=WatcherStateFile(state_path),
        sink=AtomicLandingFactsFile(facts_path),
        publisher=InMemoryWatcherFactsPublisher(),
        now=lambda: NOW,
    ).handle(ModelWatcherFactsRequest(correlation_id=FIXED_ID))
    assert result.status is EnumWatcherFactsStatus.WRITTEN, result.failure_detail
    assert result.written is not None
    return state_path, facts_path, result.written.tick


def _snapshot(
    state_path: Path, facts_path: Path
) -> tuple[LandingFactsFileSource, HandlerPrLandingSnapshot]:
    facts = LandingFactsFileSource(facts_path)
    handler = HandlerPrLandingSnapshot(
        overlay=_snapshot_overlay(),
        facts_source=facts,
        write_source=WatcherStateWriteFactsSource(
            state_path, owner=OWNER, max_age_seconds=900, now=lambda: NOW
        ),
        recorder=InMemoryLandingTickRecorder(),
        publisher=InMemoryLandingSnapshotPublisher(),
    )
    return facts, handler


def test_the_snapshot_step_records_the_tick_the_watcher_facts_node_wrote(
    tmp_path: Path,
) -> None:
    state_path, facts_path, tick = _write_facts(tmp_path)
    facts, handler = _snapshot(state_path, facts_path)

    assert facts.current_tick() == tick
    result = handler.handle(
        ModelLandingSnapshotRequest(correlation_id=FIXED_ID, tick=facts.current_tick())
    )

    assert result.status is EnumSnapshotStatus.RECORDED
    assert result.record is not None
    prs = {p.pr: p for p in result.record.facts.prs}
    assert sorted(prs) == [f"{OWNER}/omnimemory#713", f"{OWNER}/omnimemory#725"]
    assert prs[f"{OWNER}/omnimemory#713"].pr_node_id == "PR_node_omnimemory_713"
    assert prs[f"{OWNER}/omnimemory#725"].failed_run_ids == (4242,)
    assert result.node_ids_filled == 2


def test_named_break_a_renamed_facts_field_is_refused_by_the_snapshot_side(
    tmp_path: Path,
) -> None:
    state_path, facts_path, _ = _write_facts(tmp_path)
    raw = json.loads(facts_path.read_text(encoding="utf-8"))
    raw["pull_requests"] = raw.pop("prs")
    facts_path.write_text(json.dumps(raw), encoding="utf-8")
    facts, _ = _snapshot(state_path, facts_path)

    with pytest.raises(SnapshotSourceError):
        facts.current_tick()


def test_named_break_a_key_under_another_owner_fills_no_node_id(tmp_path: Path) -> None:
    state_path, facts_path, tick = _write_facts(tmp_path, owner="another-owner")
    _, handler = _snapshot(state_path, facts_path)

    result = handler.handle(
        ModelLandingSnapshotRequest(correlation_id=FIXED_ID, tick=tick)
    )

    assert result.record is not None
    assert result.node_ids_filled == 0
