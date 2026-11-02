# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Every task of a line's plan is a station with its own state and tickets, whatever shape the plan has.

Three plan shapes, one fixture each: a plan with a sibling ``.tasks.yaml`` (no waves, recorded states), a plan with
only markdown task headings, and a wave manifest with a gate. Each runs under two profiles: in memory (the plans read
with the effect contract's values, the compute handler called with a typed request) and through the effect entry with
a deployment overlay that names the lines (``ONEX_FACTORY_FLOOR_OVERLAY``). A merged pull request sets its task's
station through the merged-PR mapping the compute node already has (``floor_merged_mapping``).
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.models.model_factory_floor_input import (
    ModelFactoryFloorConfig,
    ModelFactoryFloorRequest,
    ModelFloorMerge,
    ModelFloorSelection,
    ModelFloorWatcher,
)
from omnimarket.nodes.node_factory_floor_compute.handlers.handler_factory_floor import (
    HandlerFactoryFloor,
)
from omnimarket.nodes.node_factory_floor_effect.runtime_factory_floor import main
from omnimarket.nodes.node_factory_floor_effect.source_loader import (
    load_contract,
    load_plans,
)

pytestmark = pytest.mark.unit

NODES = Path(__file__).resolve().parents[3] / "src/omnimarket/nodes"
EFFECT_CONTRACT = NODES / "node_factory_floor_effect/contract.yaml"
COMPUTE_CONTRACT = NODES / "node_factory_floor_compute/contract.yaml"
FIXTURE_KB = Path(__file__).resolve().parent / "fixtures/plan_shapes"
NOW = datetime(2026, 11, 2, 15, 0, 0, tzinfo=UTC)

# The merged pull requests the watcher records: one per plan, each naming the ticket of the plan's first task.
MERGES = (
    ("omnimarket#9001", "feat(OMN-30001): read each account's limit"),
    ("omnimarket#9002", "fix(OMN-31001): measure the context each lane loads"),
    ("omnimarket#9003", "feat(OMN-32001): capture the workflow identities"),
)

# Per shape: the line, then each station as (id, state, note, tickets, group), in plan order.
SHAPES: dict[
    str, tuple[str, tuple[tuple[str, str, str, tuple[str, ...], str], ...]]
] = {
    "tasks-yaml": (
        "2026-10-31-account-limit-monitoring-plan",
        (
            ("ALM.1", "done", "PRs merged", ("OMN-30001",), ""),
            ("ALM.2", "active", "recorded active", ("OMN-30002",), ""),
            ("ALM.3", "wait", "waits for ALM.2", ("OMN-30003",), ""),
        ),
    ),
    "headings": (
        "2026-10-30-context-plan",
        (
            ("T1", "done", "PRs merged", ("OMN-31001",), ""),
            ("T2", "prep", "ready", ("OMN-31002",), ""),
            ("T3", "prep", "ready", ("OMN-31003",), ""),
        ),
    ),
    "wave-manifest": (
        "2026-10-29-orchestration-alignment-plan",
        (
            ("W0.1", "done", "PRs merged", ("OMN-32001",), "Wave 0"),
            ("W0.2", "prep", "ready", ("OMN-32002",), "Wave 0"),
            ("G0", "wait", "waits for W0.2", (), "Wave 0"),
            ("W1.1", "wait", "waits for G0", ("OMN-32003",), "Wave 1"),
        ),
    ),
}
FEEDERS = {
    "ALM.2": ("ALM.1",),
    "ALM.3": ("ALM.2",),
    "G0": ("W0.1", "W0.2"),
    "W1.1": ("G0",),
}
LINE_CONTAINS = tuple(line for line, _ in SHAPES.values())


def _compute_config() -> ModelFactoryFloorConfig:
    raw = yaml.safe_load(COMPUTE_CONTRACT.read_text(encoding="utf-8"))
    return ModelFactoryFloorConfig.model_validate(raw["config"])


def _kb(tmp_path: Path) -> Path:
    kb = tmp_path / "kb"
    shutil.copytree(FIXTURE_KB, kb)
    return kb


def _in_memory(tmp_path: Path) -> dict[str, Any]:
    """The plans read with the effect contract's values and the compute handler called in process."""
    runtime = load_contract(EFFECT_CONTRACT).runtime
    request = ModelFactoryFloorRequest(
        generated_at=NOW,
        config=_compute_config(),
        selection=ModelFloorSelection(plan_path_contains=LINE_CONTAINS),
        plans=load_plans(
            kb=_kb(tmp_path),
            plans_relpath=runtime.plans_relpath,
            manifest_suffix=runtime.manifest_suffix,
            header_keys=runtime.plan_header_keys,
            heading_task_patterns=runtime.heading_task_patterns,
            contains=LINE_CONTAINS,
            plans_today=None,
        ),
        watcher=ModelFloorWatcher(
            last_tick=NOW,
            merges=tuple(
                ModelFloorMerge(key=key, merged_at=NOW, title=title)
                for key, title in MERGES
            ),
        ),
    )
    return HandlerFactoryFloor().handle(request).model_dump(mode="json")


def _overlay(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The effect entry with a deployment overlay naming the lines, writing the JSON the floor publishes."""
    runtime = load_contract(EFFECT_CONTRACT).runtime
    home = tmp_path / "home"
    watcher = home / runtime.sources.watcher_relpath
    watcher.parent.mkdir(parents=True)
    watcher.write_text(
        json.dumps(
            {
                "last_tick": "2026-11-02T14:59:00Z",
                "prs": {},
                "merges": {
                    key: {
                        "merged_at": "2026-11-02T14:50:00Z",
                        "title": title,
                        "head_ref": "",
                    }
                    for key, title in MERGES
                },
            }
        ),
        encoding="utf-8",
    )
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text(
        yaml.safe_dump({"plan_path_contains": list(LINE_CONTAINS)}), encoding="utf-8"
    )
    monkeypatch.setenv(runtime.overlay_file_env, str(overlay))
    ledger = tmp_path / "ledger.txt"
    ledger.write_text("", encoding="utf-8")
    argv = [
        *("--home", str(home)),
        *("--kb", str(_kb(tmp_path))),
        *("--now", "2026-11-02T15:00:00Z"),
        *("--ledger-rows-file", str(ledger)),
    ]
    assert main(argv) == 0
    document: dict[str, Any] = json.loads(
        (home / runtime.output.relpath).read_text(encoding="utf-8")
    )
    return document


PROFILES: dict[str, Callable[[Path, pytest.MonkeyPatch], dict[str, Any]]] = {
    "in-memory": lambda tmp_path, _monkeypatch: _in_memory(tmp_path),
    "overlay": _overlay,
}


@pytest.mark.parametrize("profile", sorted(PROFILES))
@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_every_task_of_the_plan_is_a_station_with_its_own_state_and_tickets(
    shape: str, profile: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    document = PROFILES[profile](tmp_path, monkeypatch)
    line_id, expected = SHAPES[shape]
    lines = {line["line_id"]: line for line in document["lines"]}
    line = lines[line_id]
    assert line["source"] == "manifest"
    stations = line["stations"]
    assert [
        (
            st["id"],
            st["state"],
            st["items"][0]["note"],
            tuple(st["tickets"]),
            st["group"],
        )
        for st in stations
    ] == list(expected)
    for st in stations:
        # One station per task: the station is the task, its state the task's own.
        assert [item["id"] for item in st["items"]] == [st["id"]]
        assert st["state"] == st["items"][0]["state"]
        assert tuple(st["feeders"]) == FEEDERS.get(st["id"], ())
    # The merged pull request reached its task's station through the merged-PR mapping, and nowhere else.
    merged = {key for key, _ in MERGES}
    assert [st["id"] for st in stations if set(st["items"][0]["prs"]) & merged] == [
        expected[0][0]
    ]


def test_a_recorded_done_without_a_merged_pull_request_is_not_built(
    tmp_path: Path,
) -> None:
    """A tasks.yaml may record a task done; the station is done only once its pull request merged."""
    kb = _kb(tmp_path)
    runtime = load_contract(EFFECT_CONTRACT).runtime
    plans = load_plans(
        kb=kb,
        plans_relpath=runtime.plans_relpath,
        manifest_suffix=runtime.manifest_suffix,
        header_keys=runtime.plan_header_keys,
        heading_task_patterns=runtime.heading_task_patterns,
        contains=("account-limit-monitoring",),
        plans_today=None,
    )
    result = HandlerFactoryFloor().handle(
        ModelFactoryFloorRequest(
            generated_at=NOW,
            config=_compute_config(),
            plans=plans,
            watcher=ModelFloorWatcher(last_tick=NOW),
        )
    )
    first = result.lines[0].stations[0]
    assert (first.id, first.state, first.items[0].note) == (
        "ALM.1",
        "wait",
        "recorded done, no merged pull request read",
    )


def test_the_heading_rule_is_the_contracts_not_the_loaders(tmp_path: Path) -> None:
    """Without the contract's markdown heading pattern the headings plan reads no task: the rule lives in the contract."""
    runtime = load_contract(EFFECT_CONTRACT).runtime
    (bold_only, *_) = runtime.heading_task_patterns
    (plan,) = load_plans(
        kb=_kb(tmp_path),
        plans_relpath=runtime.plans_relpath,
        manifest_suffix=runtime.manifest_suffix,
        header_keys=runtime.plan_header_keys,
        heading_task_patterns=(bold_only,),
        contains=("context-plan",),
        plans_today=None,
    )
    assert plan.manifest is None


def test_wave_grouping_stays_a_contract_choice() -> None:
    """The contract groups by task; the wave grouping is the one other value it may declare."""
    assert _compute_config().station_grouping == "task"
