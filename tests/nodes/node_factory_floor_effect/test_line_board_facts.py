# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Lean line board T3 (OMN-22368): the floor effect forwards facts and source health to the compute.

Each test runs the effect over recorded sources (the ``world`` fixture of test_runtime_factory_floor plus the goal
file, the saved required contexts and an archive roll) and reads what reached the compute request and the record.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

import omnimarket.nodes.node_factory_floor_unresolved_effect as unresolved_step
from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.models.model_factory_floor_input import ModelFactoryFloorRequest
from omnimarket.models.model_merged_running_count import (
    ModelMergedRunningCountResult,
    ModelMrcEvidence,
    ModelMrcEvidenceRead,
)
from omnimarket.nodes.node_factory_floor_compute.handlers import handler_factory_floor
from omnimarket.nodes.node_factory_floor_effect import source_loader
from omnimarket.nodes.node_factory_floor_effect.handlers import (
    handler_factory_floor_effect as effect_module,
)
from omnimarket.nodes.node_factory_floor_effect.handlers.handler_factory_floor_effect import (
    HandlerFactoryFloorEffect,
)
from omnimarket.nodes.node_factory_floor_effect.handlers.source_health import (
    source_health,
)
from omnimarket.nodes.node_factory_floor_effect.models.model_factory_floor_run import (
    ModelFactoryFloorRunRequest,
)
from omnimarket.nodes.node_factory_floor_effect.source_loader import (
    SourceRefusedError,
    load_contract,
)
from omnimarket.nodes.node_factory_floor_unresolved_effect.handlers import (
    HandlerFactoryFloorUnresolved,
)
from omnimarket.nodes.node_factory_floor_unresolved_effect.models.model_factory_floor_unresolved import (
    ModelFactoryFloorUnresolvedOverlay,
    ModelFactoryFloorUnresolvedRequest,
)

from .test_runtime_factory_floor import CONTRACT, NOW, _write, world
from .window_port import RecordedWindowPort

pytestmark = pytest.mark.unit

__all__ = ["world"]

SHA = "dead74d017754dcb79a60bfb09d9d4b7c9255713"
OVERLAY = {
    "plan_path_contains": ["beta/plans/2026-10-"],
    "processes": [
        {
            "name": "lab-fill",
            "actor": "script:lab-fill",
            "run_kind": "STATUS",
            "run_match": "run=",
            "did_nothing_match": "eligible=0",
            "interval_min": 10,
            "outcomes_covered": ["success", "did-nothing", "error"],
        }
    ],
    "roles": ["operator"],
    "release_required_repos": ["example-repo"],
    "held_gates": ["Hostile Review Gate"],
}
WINDOW_ROWS = "\n".join(
    [
        "L1 2026-10-31T11:40:00Z | CLAIM | lane=lane-a | ticket=OMN-100 | task=W0.2 | claim text",
        "L2 2026-10-31T11:41:00Z | RULING | lane=orch | ticket=OMN-17427 | mechanism-ticket=OMN-23025 | a ruling",
        "L3 2026-10-31T11:42:00Z | FRICTION | lane=lane-a | ticket=OMN-100 | a friction row",
        "L4 2026-10-31T11:43:00Z | RELEASE | lane=lane-a | ticket=OMN-100 | pr=kb#2 | head="
        + SHA
        + " | released",
        "L5 2026-10-31T11:44:00Z | CORRECTION | lane=orch | ticket=OMN-100 | a correction",
        "L6 2026-10-31T11:45:00Z | ACK | lane=lane-b | a plain acknowledgement",
        "L7 2026-10-31T11:46:00Z | MSG | from=lane-b | a message with no recipient",
    ]
)
# Rows only an archive roll older than the 14-day window holds.
ARCHIVE_ROLL = "\n".join(
    [
        "L90 2026-09-02T08:00:00Z | TERMINAL | lane=old-blocked | ticket=OMN-5 | outcome=blocked | blocker=waiting on a person",
        "L91 2026-09-03T08:00:00Z | STATUS | lane=old-hand | ticket=OMN-6 | pr=kb#9 | handed-off=kb#9 | handed to landing",
        "L92 2026-09-04T08:00:00Z | TERMINAL | lane=old-done | ticket=OMN-7 | outcome=done | finished",
        "L93 2026-09-05T08:00:00Z | STATUS | lane=old-plain | ticket=OMN-8 | state=gate | an ordinary status",
    ]
)
GOAL = (
    "state_as_of: 2026-10-31T10:00:00Z\n"
    "Format: id | what | verdict | next action | probe. Sub-row: -> IR id | destination\n"
    "G4 | Beta | OPEN | close G1 (OMN-16632) | all gate rows done\n"
    "P1 | Good local answer ends the chain | LANDED | refresh then probe | onex delegate alive\n"
    "-> I4 | dev lane | run onex delegate | receipt | hold: none\n"
    "B1 | Non-us person signs in | UNKNOWN | run browser sign-in (OMN-16632) | browser sign-in\n"
)
REQUIRED = {
    "omnimarket": ["CI Summary", "Hostile Review Gate"],
    "kb:main": {"contexts": ["validate"], "read_at": "2026-10-31T11:30:00Z"},
}


class _Reader:
    """A bound merged-running reader: the count raises (unread), the evidence is supplied."""

    def __init__(self, read: ModelMrcEvidenceRead | None = None) -> None:
        self._read = read

    def read(
        self, now: datetime, *, hosts_table: Path | None = None
    ) -> ModelMergedRunningCountResult:
        raise OSError("the count is not under test")

    def read_evidence(
        self, now: datetime, *, hosts_table: Path | None = None
    ) -> ModelMrcEvidenceRead:
        assert self._read is not None
        return self._read


EVIDENCE = ModelMrcEvidenceRead(
    evidence=(
        ModelMrcEvidence(
            repo="OmniNode-ai/omnimarket",
            kind="adoption",
            source="ledger",
            number=7,
            observed_at=NOW,
        ),
    )
)


# The unpatched calls, so a second recording inside one test does not record into the first.
ORIGINAL_OPEN = pathlib.Path.open
ORIGINAL_RUN = subprocess.run


def _prepare(
    world: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> dict[str, Path]:
    """Recorded sources for every key of the line board, an overlay with the declarations, and the files that stand in for the ledger."""
    home, kb, tmp = world["home"], world["kb"], world["tmp"]
    scratch = home / ".claude_scratch"
    watcher = json.loads((scratch / "pr-watcher/state.json").read_text())
    watcher["prs"]["kb#2"]["facts"]["head_sha"] = SHA
    watcher["prs"]["kb#2"]["ci"] = {
        "sha": SHA,
        "verdict": "RED",
        "red": ["Hostile Review Gate"],
        "total": 3,
        "runs": [
            ["CI Summary", "completed", "success", "2026-10-31T11:50:00Z"],
            ["Hostile Review Gate", "completed", "failure", "2026-10-31T11:51:00Z"],
            ["validate", "in_progress", "", "2026-10-31T11:52:00Z"],
        ],
    }
    _write(scratch / "pr-watcher/state.json", watcher)
    required = _write(scratch / "queue-status/required-contexts.json", REQUIRED)
    goal = _write(kb / "beta/GOAL.md", GOAL)
    window = _write(tmp / "window-rows.txt", WINDOW_ROWS)
    archive_roll = _write(tmp / "archive-roll.txt", ARCHIVE_ROLL)
    # The unresolved step's own record, made by the step over the archive roll: the floor reads what the step writes (seam S1).
    unresolved_overlay = ModelFactoryFloorUnresolvedOverlay.model_validate(
        yaml.safe_load(
            (Path(unresolved_step.__file__).parent / "overlay.example.yaml").read_text()
        )
    )
    archive = tmp / "unresolved-rows.json"
    archive.write_text(
        HandlerFactoryFloorUnresolved(unresolved_overlay, clock=lambda: NOW)
        .handle(
            ModelFactoryFloorUnresolvedRequest(
                read_at=NOW, stand_in_file=str(archive_roll)
            )
        )
        .model_dump_json(),
        encoding="utf-8",
    )
    overlay = _write(tmp / "overlay.yaml", yaml.safe_dump(OVERLAY))
    monkeypatch.setenv("ONEX_FACTORY_FLOOR_OVERLAY", str(overlay))
    # Every source is as fresh as the run's clock.
    for path in (required, goal, window, archive):
        os.utime(path, (NOW.timestamp(), NOW.timestamp()))
    for path in (scratch / "plans-today").rglob("*"):
        if path.is_file():
            os.utime(path, (NOW.timestamp(), NOW.timestamp()))
    return {
        "required": required,
        "goal": goal,
        "window": window,
        "archive": archive,
        "overlay": overlay,
    }


def _run(
    world: dict[str, Path],
    files: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    reader: _Reader | None = None,
) -> tuple[ModelFactoryFloorRequest, dict[str, Any]]:
    """Run the effect and return the request the compute received and the record written."""
    seen: list[ModelFactoryFloorRequest] = []

    class Recording(handler_factory_floor.HandlerFactoryFloor):
        def handle(self, request: ModelFactoryFloorRequest) -> Any:
            seen.append(request)
            return super().handle(request)

    monkeypatch.setattr(effect_module, "HandlerFactoryFloor", Recording)
    result = HandlerFactoryFloorEffect(
        merged_running=reader or _Reader(EVIDENCE)
    ).handle(
        ModelFactoryFloorRunRequest(
            now=NOW,
            home=str(world["home"]),
            kb=str(world["kb"]),
            ledger_rows_file=str(files["window"]),
            unresolved_rows_file=str(files["archive"]),
        )
    )
    return seen[0], json.loads(Path(result.output_path).read_text())


def test_facts_reach_the_request_and_the_health_reaches_the_record(
    world: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    request, record = _run(world, _prepare(world, monkeypatch), monkeypatch)
    pr = next(pr for pr in request.watcher.prs if pr.key == "kb#2")
    assert pr.head_sha == SHA
    assert {c.name: c.result for c in pr.contexts} == {
        "CI Summary": "success",
        "Hostile Review Gate": "failure",
        "validate": "in_progress",
    }
    kinds = {row.kind for row in request.ledger}
    assert {"RULING", "FRICTION", "CORRECTION", "ACK", "MSG", "RELEASE"} <= kinds
    release = next(row for row in request.ledger if row.kind == "RELEASE")
    assert release.head == SHA
    ruling = next(row for row in request.ledger if row.kind == "RULING")
    assert ruling.mechanism == "OMN-23025"
    # The archive roll: a blocked TERMINAL and a handed-off STATUS older than the window reach the request, and only those.
    assert request.ledger_unresolved is not None
    assert {(row.kind, row.lane) for row in request.ledger_unresolved} == {
        ("TERMINAL", "old-blocked"),
        ("STATUS", "old-hand"),
    }
    assert all(row.ts < NOW - timedelta(days=14) for row in request.ledger_unresolved)
    assert request.running_evidence == EVIDENCE.evidence
    assert [(c.repo, c.contexts) for c in request.required_contexts or ()] == [
        ("kb", ("validate",)),
        ("omnimarket", ("CI Summary", "Hostile Review Gate")),
    ]
    assert [row.row_id for row in request.goal_rows or ()] == ["G4", "B1"]
    assert request.selection.plans_today_paths == ("beta/plans/2026-10-01-old-plan.md",)
    assert [p.name for p in request.processes] == ["lab-fill"]
    assert request.roles == ("operator",)
    assert request.release_required_repos == ("example-repo",)
    assert request.held_gates == ("Hostile Review Gate",)
    # The record carries exactly the health the request carried, one entry per source the board depends on.
    assert record["sources"]["health"] == [
        json.loads(entry.model_dump_json()) for entry in request.source_health
    ]
    bars = load_contract(CONTRACT).runtime.source_max_age_min
    assert [entry.source for entry in request.source_health] == list(
        type(bars).model_fields
    )
    assert {entry.state for entry in request.source_health} == {"read"}


def test_a_stop_kept_only_in_an_archive_roll_comes_from_the_unresolved_steps_record(
    world: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The floor reads the ledger once, for the window; the four unresolved reads are the unresolved step's (OMN-19998).

    Break: the floor reading the four unresolved reads itself again (the base) reads more than the window's declared page reads, and took 231 s.
    """
    files = _prepare(world, monkeypatch)
    port = RecordedWindowPort(WINDOW_ROWS)
    record = (
        world["home"] / load_contract(CONTRACT).runtime.sources.unresolved_rows_relpath
    )
    record.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(files["archive"], record)
    seen: list[ModelFactoryFloorRequest] = []

    class Recording(handler_factory_floor.HandlerFactoryFloor):
        def handle(self, request: ModelFactoryFloorRequest) -> Any:
            seen.append(request)
            return super().handle(request)

    monkeypatch.setattr(effect_module, "HandlerFactoryFloor", Recording)
    HandlerFactoryFloorEffect(
        merged_running=_Reader(EVIDENCE), ledger_source=port
    ).handle(
        ModelFactoryFloorRunRequest(
            now=NOW, home=str(world["home"]), kb=str(world["kb"])
        )
    )
    # Every request is one of the window's declared page reads; none asks for the history older than the window.
    declared = {r.name for r in load_contract(CONTRACT).runtime.ledger_pages.reads}
    assert port.requests
    assert len(declared) > 1
    assert all(q.query == "rows" and q.since is not None for q in port.requests)
    unresolved = seen[0].ledger_unresolved
    assert unresolved is not None
    assert {row.lane for row in unresolved} == {"old-blocked", "old-hand"}


@pytest.mark.parametrize("cause", ["missing", "stale"])
def test_an_unreadable_unresolved_record_makes_the_ledger_unread_never_empty(
    world: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    cause: str,
) -> None:
    files = _prepare(world, monkeypatch)
    port = RecordedWindowPort(WINDOW_ROWS)
    runtime = load_contract(CONTRACT).runtime
    if cause == "stale":
        record = world["home"] / runtime.sources.unresolved_rows_relpath
        record.parent.mkdir(parents=True, exist_ok=True)
        # A record read longer ago than the floor's bound: a stop kept only in an archive roll may have been missed.
        text = files["archive"].read_text(encoding="utf-8")
        old = (NOW - timedelta(hours=runtime.unresolved_max_age_hours + 1)).isoformat()
        document = json.loads(text)
        document["read_at"] = old
        record.write_text(json.dumps(document), encoding="utf-8")
    seen: list[ModelFactoryFloorRequest] = []

    class Recording(handler_factory_floor.HandlerFactoryFloor):
        def handle(self, request: ModelFactoryFloorRequest) -> Any:
            seen.append(request)
            return super().handle(request)

    monkeypatch.setattr(effect_module, "HandlerFactoryFloor", Recording)
    HandlerFactoryFloorEffect(
        merged_running=_Reader(EVIDENCE), ledger_source=port
    ).handle(
        ModelFactoryFloorRunRequest(
            now=NOW, home=str(world["home"]), kb=str(world["kb"])
        )
    )
    assert seen[0].ledger_unresolved is None
    ledger = next(h for h in seen[0].source_health if h.source == "ledger")
    assert ledger.state == "unread"
    assert "archive rolls unread" in ledger.cause


def _opened(
    world: dict[str, Path],
    files: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    extra: Callable[[], None] | None = None,
) -> tuple[set[str], list[str]]:
    """The files the run opens (relative to the temporary root) and the programs it starts."""
    root = str(world["tmp"])
    opened: set[str] = set()
    started: list[str] = []
    real_open = ORIGINAL_OPEN
    real_run = ORIGINAL_RUN

    def recording_open(self: Path, *args: Any, **kwargs: Any) -> Any:
        mode = args[0] if args else kwargs.get("mode", "r")
        if str(self).startswith(root) and "r" in mode:
            opened.add(str(self).removeprefix(root + "/"))
        return real_open(self, *args, **kwargs)

    def recording_run(argv: Any, *args: Any, **kwargs: Any) -> Any:
        started.append(Path(str(argv[0])).name)
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "open", recording_open)
    monkeypatch.setattr(subprocess, "run", recording_run)
    if extra is not None:
        extra()
    _run(world, files, monkeypatch)
    return opened, started


# The files the effect opened before OMN-22368 over these recorded sources (relative to the temporary root): the
# overlay, the ledger rows file, the plans and their manifests, the watcher state, the plans-today output and the Linear
# snapshot, the friction report, the placement reading and the lane, limit, drift and fleet sources (several are absent
# in the recorded world and are still opened), and the previous record the writer reads.
BASE_OPENED = {
    "overlay.yaml",
    "window-rows.txt",
    "no-such-hosts.yaml",
    "remote-lanes/hosts-probe/latest.json",
    "kb/beta/plans/2026-10-01-old-plan.md",
    "kb/beta/plans/2026-10-31-wave-plan.md",
    "kb/beta/plans/2026-10-31-wave-plan.tasks.yaml",
    "home/.claude_scratch/pr-watcher/state.json",
    "home/.claude_scratch/plans-today/2026-10-31/plans-today.json",
    "home/.claude_scratch/plans-today/linear-2026-10-31.txt",
    "home/.claude_scratch/lab-run/hosts.json",
    "home/.claude_scratch/friction/friction-report.json",
    "home/.claude_scratch/limits/api-credit-meter.json",
    "home/.claude_scratch/limits/engines.json",
    "home/.claude_scratch/factory-floor/factory-floor.json",
    "home/.onex_state/host-drift/fleet.json",
    "home/.onex_state/runner-fleet/runner-fleet.json",
}


def test_the_effect_opens_nothing_it_did_not_open_before_except_the_three_new_sources(
    world: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    files = _prepare(world, monkeypatch)
    opened, started = _opened(world, files, monkeypatch)
    relative = {
        k: str(v).removeprefix(str(world["tmp"]) + "/") for k, v in files.items()
    }
    new = {relative["goal"], relative["required"], relative["archive"]}
    base = opened - new
    # The base set: the plans, manifests, watcher, plans-today output, Linear snapshots, friction report, placement
    # reading, overlay, ledger rows file and nothing else. A new source would be a member of this difference.
    assert new <= opened
    assert base == BASE_OPENED
    # No service is started that the base did not start: the cloud account node, if anything.
    assert set(started) <= {"onex", "python", "python3", Path(sys.executable).name}


def test_the_read_recorder_sees_a_source_the_effect_should_not_open(
    world: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failing control: an injected extra read changes the recorded set, so the comparison above can fail."""
    files = _prepare(world, monkeypatch)
    stray = _write(world["tmp"] / "stray.txt", "x")
    plain, _ = _opened(world, files, monkeypatch)
    real_goal = source_loader.load_goal_rows

    def breaking(path: Path, closed: Any) -> Any:
        stray.read_text()
        return real_goal(path, closed)

    monkeypatch.setattr(effect_module, "load_goal_rows", breaking)
    broken, _ = _opened(world, files, monkeypatch)
    assert broken - plain == {"stray.txt"}


def _health(
    world: dict[str, Path], files: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> dict[str, tuple[str, str]]:
    request, _ = _run(world, files, monkeypatch)
    return {h.source: (h.state, h.cause) for h in request.source_health}


def test_unread_read_empty_and_stale_are_three_different_states(
    world: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    files = _prepare(world, monkeypatch)
    # unread: the saved required-contexts map is gone.
    files["required"].unlink()
    # read-empty: the goal file is read and every row is closed.
    files["goal"].write_text("G1 | Done | LANDED | none | probe\n", encoding="utf-8")
    # stale: the Linear snapshot is older than its bar.
    for snapshot in (world["home"] / ".claude_scratch/plans-today").glob(
        "linear-*.txt"
    ):
        old = (NOW - timedelta(days=3)).timestamp()
        os.utime(snapshot, (old, old))
    states = _health(world, files, monkeypatch)
    assert states["required_contexts"][0] == "unread"
    assert "required-contexts.json" in states["required_contexts"][1]
    assert states["goal"][0] == "read-empty"
    assert states["linear"][0] == "stale"
    assert "bar 1440 min" in states["linear"][1]
    assert (
        len({states["required_contexts"][0], states["goal"][0], states["linear"][0]})
        == 3
    )
    # The sources that were not touched still read.
    assert states["watcher"][0] == states["plans"][0] == "read"


def test_source_health_keeps_four_states_apart() -> None:
    def judge(read_at: datetime | None, count: int | None) -> str:
        return source_health(
            source="s", now=NOW, max_age_min=60, read_at=read_at, count=count
        ).state

    assert judge(NOW, 3) == "read"
    assert judge(NOW, 0) == "read-empty"
    assert judge(NOW - timedelta(minutes=61), 3) == "stale"
    assert judge(NOW - timedelta(minutes=61), 0) == "stale"
    assert judge(NOW, None) == "unread"
    assert judge(None, 3) == "unread"


def test_running_evidence_read_in_part_is_unread_not_complete(
    world: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    files = _prepare(world, monkeypatch)
    partial = _Reader(
        ModelMrcEvidenceRead(unread_cause="deployed: lab-pass receipt absent")
    )
    request, _ = _run(world, files, monkeypatch, reader=partial)
    assert request.running_evidence is None
    entry = next(h for h in request.source_health if h.source == "running_evidence")
    assert (entry.state, entry.cause) == ("unread", "deployed: lab-pass receipt absent")


def test_a_reader_with_no_evidence_method_leaves_running_evidence_unread(
    world: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    class CountOnly:
        def read(self, now: datetime, *, hosts_table: Path | None = None) -> Any:
            raise OSError("not under test")

    files = _prepare(world, monkeypatch)
    request, _ = _run(world, files, monkeypatch, reader=cast(Any, CountOnly()))
    entry = next(h for h in request.source_health if h.source == "running_evidence")
    assert entry.state == "unread"
    assert "no per-merge evidence" in entry.cause


@pytest.mark.parametrize("key", sorted(OVERLAY.keys() - {"plan_path_contains"}))
def test_each_overlay_key_fails_closed_naming_the_key(
    key: str, world: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    files = _prepare(world, monkeypatch)
    files["overlay"].write_text(
        yaml.safe_dump({k: v for k, v in OVERLAY.items() if k != key}),
        encoding="utf-8",
    )
    with pytest.raises(NodeOverlayError, match=key):
        _run(world, files, monkeypatch)


def _contract_keys() -> list[tuple[str, ...]]:
    runtime = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))["runtime"]
    keys: list[tuple[str, ...]] = [
        ("ledger_kinds",),
        ("unresolved_max_age_hours",),
        ("sources", "unresolved_rows_relpath"),
        ("goal_relpath",),
        ("goal_closed_verdicts",),
        ("sources", "required_contexts_relpath"),
        ("source_max_age_min",),
    ]
    keys += [("source_max_age_min", name) for name in runtime["source_max_age_min"]]
    return keys


@pytest.mark.parametrize("path", _contract_keys(), ids=lambda p: ".".join(p))
def test_each_contract_key_fails_closed_naming_the_key(
    path: tuple[str, ...],
    world: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    files = _prepare(world, monkeypatch)
    document = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    node: Any = document["runtime"]
    for part in path[:-1]:
        node = node[part]
    del node[path[-1]]
    broken = tmp_path / "broken-contract.yaml"
    broken.write_text(yaml.safe_dump(document), encoding="utf-8")
    monkeypatch.setattr(effect_module, "OWN_CONTRACT", broken)
    with pytest.raises(SourceRefusedError, match=path[-1]):
        _run(world, files, monkeypatch)
    # Positive control: the unbroken contract runs.
    shutil.copy(CONTRACT, broken)
    _run(world, files, monkeypatch)
