# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Replay of the recorded 2026-11-23 transition: C29 and C14 went red on dev builds, from the probes' own recorded runs."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from omnimarket.models.overlay_documents.model_build_delegation_checks_overlay import (
    ModelBuildDelegationChecksOverlay,
)
from omnimarket.models.test_selector_reading import ModelAttributionCandidate
from omnimarket.nodes.node_build_delegation_checks.adapters.git_merge_source import (
    parse_log,
)
from omnimarket.nodes.node_build_delegation_checks.adapters.inmemory_adapters import (
    InMemoryBuildStore,
    RecordedResultsExecutor,
    RecordingSink,
)
from omnimarket.nodes.node_build_delegation_checks.handlers.handler_build_delegation_checks import (
    HandlerBuildDelegationChecks,
)
from omnimarket.nodes.node_build_delegation_checks.models.model_build_delegation_checks import (
    EnumCheckReading,
    ModelCheckOutcome,
    ModelRebuildCompletedObserved,
)

FIXTURE = Path(__file__).parent / "fixtures" / "recorded_transition_2026_11_23.json"
FIRST_PARENT_FIRST = "7f5e3a1a"


def _at(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


class _HistoryMergeSource:
    """The first-parent history of the fixture: the commits after ``base`` up to ``head``, oldest first."""

    def __init__(self, history: tuple[ModelAttributionCandidate, ...]) -> None:
        self._shas = [c.commit_sha for c in history]
        self._by_sha = {c.commit_sha: c for c in history}

    def merges_between(
        self, base_sha: str, head_sha: str
    ) -> tuple[ModelAttributionCandidate, ...]:
        lo, hi = self._shas.index(base_sha), self._shas.index(head_sha)
        return tuple(self._by_sha[s] for s in self._shas[lo + 1 : hi + 1])


def _replay() -> tuple[RecordingSink, tuple[ModelAttributionCandidate, ...], list[str]]:
    doc = json.loads(FIXTURE.read_text(encoding="utf-8"))
    history = parse_log("\n".join(doc["dev_log"]))
    committed = {c.commit_sha: c.merged_at for c in history}
    builds: list[str] = doc["builds"]
    recorded: dict[str, dict[str, ModelCheckOutcome]] = {b: {} for b in builds}
    for check_id, runs in doc["runs"].items():
        for run in runs:
            finished = _at(run["finished_at"])
            # The run read the newest build whose commit time is at or before it finished.
            taken = [b for b in builds if committed[b] <= finished]
            if not taken:
                continue
            recorded[taken[-1]][check_id] = ModelCheckOutcome(
                reading=EnumCheckReading.PASS
                if run["verdict"] == "pass"
                else EnumCheckReading.FAIL,
                run_id=run["run_id"],
                finished_at=finished,
            )
    sink = RecordingSink()
    handler = HandlerBuildDelegationChecks(
        settings=ModelBuildDelegationChecksOverlay(owner="delegation-owner"),
        runner=RecordedResultsExecutor(recorded),
        store=InMemoryBuildStore(),
        merges=_HistoryMergeSource(history),
        sink=sink,
        clock=lambda: _at("2026-11-23T15:00:00Z"),
    )
    for build in builds:
        handler.handle(ModelRebuildCompletedObserved(git_sha=build, runtime_lane="dev"))
    return sink, history, builds


def test_replay_1330_1445() -> None:
    sink, history, builds = _replay()
    pr_of = {c.commit_sha: c.pr_number for c in history}
    short = {s[:8]: s for s in builds}

    by_check = {e.check_id: e for e in sink.regressions}
    # Exactly the two real transitions: no other check of the ten went from pass to fail in the window.
    assert sorted(by_check) == ["C14", "C29"]

    c29 = by_check["C29"]  # 13:00:21Z pass on c13764f9, 14:00:35Z fail on f42e0c13
    assert c29.last_green.run_id == "45179185106"
    assert c29.first_red.run_id == "45189064181"
    assert c29.previous_build_id == short["c13764f9"]
    assert c29.build_id == short["f42e0c13"]
    assert [s.pr_number for s in c29.suspects] == [6641, 6616, 6627, 6618]
    assert [s.commit_sha[:8] for s in c29.suspects] == [
        "831b18bf",
        "7bad8086",
        "a2ee4349",
        "f42e0c13",
    ]

    c14 = by_check["C14"]  # 13:39:31Z pass on f42e0c13, 14:39:17Z fail on 5b1931de
    assert c14.last_green.run_id == "45185798731"
    assert c14.first_red.run_id == "45196274344"
    assert c14.previous_build_id == short["f42e0c13"]
    assert c14.build_id == short["5b1931de"]
    assert [s.pr_number for s in c14.suspects] == [6635]
    assert pr_of[c14.suspects[0].commit_sha] == 6635

    # C29 stayed red on the build after: no second event. One alarm per regression, each owned.
    assert len(sink.regressions) == 2
    assert len(sink.alarms) == 2
    assert all(e.owner == "delegation-owner" for e in sink.regressions)
    assert all("delegation-owner" in a.alarms[0].detail for a in sink.alarms)
