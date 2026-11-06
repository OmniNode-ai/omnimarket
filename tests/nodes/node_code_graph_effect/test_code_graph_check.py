# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The pull request check: a head that adds a module no root reaches goes red (OMN-22651).

This is the plan's second injected fault for piece 1: plant an unreachable module in a test
repository on a pull request head, and the check goes red. It runs through the node's own CLI on
real git clones, so the verb, the overlay load and the git backend are the ones the lab runs.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_code_graph_effect.runtime_code_graph import main
from tests.nodes.node_code_graph_effect.code_graph_support import (
    APP,
    CORE,
    EXAMPLE,
    commit,
    make_clone,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    clones = tmp_path / "clones"
    make_clone(clones, "example_core", CORE)
    make_clone(clones, "example_app", APP)
    roots = tmp_path / "overlays"
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    document["graph_backend"] = "git"
    target = roots / "node_code_graph_effect" / "overlay.yaml"
    target.parent.mkdir(parents=True)
    target.write_text(yaml.safe_dump(document), encoding="utf-8")
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(roots))
    monkeypatch.setenv("EXAMPLE_REPOS_ROOT", str(clones))
    return clones / "example_app"


def _check(capsys: pytest.CaptureFixture[str], base: str, head: str) -> tuple[int, str]:
    code = main(["check", "--repo", "example_app", "--base", base, "--head", head])
    return code, capsys.readouterr().out


def test_a_planted_unreachable_module_fails_the_check(
    world: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    head = commit(world, {"src/example_app/planted.py": "PLANTED = 1\n"}, "plant")
    code, out = _check(capsys, "origin/main", head)
    assert code == 1, out
    assert "UNREACHABLE-ADDED example_app src/example_app/planted.py" in out
    assert "verdict=fail" in out


def test_a_module_the_handler_imports_passes_the_check(
    world: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    head = commit(
        world,
        {
            "src/example_app/nodes/node_a/extra.py": "EXTRA = 1\n",
            "src/example_app/nodes/node_a/handler.py": APP[
                "src/example_app/nodes/node_a/handler.py"
            ]
            + "from .extra import EXTRA\n",
        },
        "wired",
    )
    code, out = _check(capsys, "origin/main", head)
    assert code == 0, out
    assert "added=1 flagged=0 verdict=pass" in out


def test_an_added_test_file_is_not_flagged(
    world: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    head = commit(world, {"tests/test_orphan.py": "def test_x():\n    pass\n"}, "test")
    code, out = _check(capsys, "origin/main", head)
    assert code == 0, out


def test_the_base_against_itself_passes(
    world: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out = _check(capsys, "origin/main", "origin/main")
    assert code == 0, out
    assert "added=0" in out


def test_an_unknown_ref_is_indeterminate(
    world: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(
        ["check", "--repo", "example_app", "--base", "origin/main", "--head", "nope"]
    )
    assert code == 3
