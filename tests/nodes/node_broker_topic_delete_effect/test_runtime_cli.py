# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The delete node's command line (OMN-23879): a removal plan's result becomes the command, the run
prints one RESULT and one ENTRY line per topic, and its exit code says whether the run failed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.models.model_topic_removal import (
    EnumRemovalMode,
    ModelTopicRemovalCommand,
)
from omnimarket.nodes.node_broker_topic_delete_effect import (
    runtime_broker_topic_delete as runtime,
)
from tests.nodes.node_broker_topic_delete_effect.delete_support import (
    TOPICS,
    World,
    overlay,
)
from tests.nodes.node_broker_topic_removal_effect.removal_support import (
    World as RemovalWorld,
)

pytestmark = pytest.mark.unit


def _plan_file(tmp_path: Path) -> tuple[Path, list[str]]:
    """A real removal plan's result JSON, and the candidate names it holds."""
    world = RemovalWorld()
    world.break_make_declared_removable()
    plan = world.handler().handle(
        ModelTopicRemovalCommand(correlation_id="cli-1", mode=EnumRemovalMode.DRY_RUN)
    )
    path = tmp_path / "plan.json"
    path.write_text(plan.model_dump_json(), encoding="utf-8")
    return path, [candidate.name for candidate in plan.candidates]


def _wire(monkeypatch: pytest.MonkeyPatch, world: World, *, enforce: bool) -> None:
    monkeypatch.setattr(runtime, "load_overlay", lambda: overlay(enforce=enforce))
    monkeypatch.setattr(
        runtime, "build_handler", lambda _overlay, _env: world.handler()
    )


def test_a_plan_file_runs_report_only_and_prints_one_entry_per_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plan, names = _plan_file(tmp_path)
    world = World()
    _wire(monkeypatch, world, enforce=False)
    out = tmp_path / "result.json"
    code = runtime.main(["run", "--plan-file", str(plan), "--out", str(out)])
    printed = capsys.readouterr().out
    assert code == 0
    assert "RESULT status=report_only deletes_issued=0 refusal=-" in printed
    assert printed.count("ENTRY ") == len(names)
    assert world.admin.calls == []
    assert out.is_file()


def test_an_enforcing_run_without_approval_exits_one_and_names_the_refusal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plan, _ = _plan_file(tmp_path)
    world = World(enforce=True)
    _wire(monkeypatch, world, enforce=True)
    code = runtime.main(["run", "--plan-file", str(plan)])
    assert code == 1
    assert "refusal=approval_missing" in capsys.readouterr().out
    assert world.admin.calls == []


def test_a_command_file_that_is_not_a_command_exits_three(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text('{"topics": []}', encoding="utf-8")
    assert runtime.main(["run", "--command-file", str(bad)]) == 3
    assert "INDETERMINATE" in capsys.readouterr().err


def test_the_command_from_a_plan_names_exactly_its_candidates() -> None:
    world = RemovalWorld()
    world.break_make_declared_removable()
    plan = world.handler().handle(
        ModelTopicRemovalCommand(correlation_id="cli-2", mode=EnumRemovalMode.DRY_RUN)
    )
    command = runtime.command_from_plan(
        plan, approval_ref=None, ruling_covers_declared=False, ruling_ref=None
    )
    assert [target.name for target in command.topics] == [
        c.name for c in plan.candidates
    ]
    assert TOPICS["clean"] not in {target.name for target in command.topics}
