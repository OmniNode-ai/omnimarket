# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC1 (OMN-22902): the check goes red on a planted unreachable module and green on the same head without it.

It runs through the node's own CLI on real git clones, so the verb, the overlay load and the git
backend are the ones the lab runs. The failing control shows the verdict follows the graph: with the
contract links dropped (the node's declared fault) a module that is wired through its contract reads
unreachable and the same check goes red on it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_code_graph_effect.handlers.graph_compute import (
    GraphRules,
    check_added,
)
from omnimarket.nodes.node_code_graph_effect.runtime_code_graph import main
from tests.nodes.node_code_graph_effect.code_graph_support import (
    APP,
    CORE,
    EXAMPLE,
    commit,
    make_clone,
    overlay,
    source,
)
from tests.unit.nodes.node_code_graph_effect.pr_support import PLANTED, WIRED

pytestmark = pytest.mark.unit

BASE = "origin/main"


@pytest.fixture
def clone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    clones = tmp_path / "clones"
    make_clone(clones, "example_core", CORE)
    make_clone(clones, "example_app", APP)
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    document["graph_backend"] = "git"
    target = tmp_path / "overlays" / "node_code_graph_effect" / "overlay.yaml"
    target.parent.mkdir(parents=True)
    target.write_text(yaml.safe_dump(document), encoding="utf-8")
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path / "overlays"))
    monkeypatch.setenv("EXAMPLE_REPOS_ROOT", str(clones))
    return clones / "example_app"


def _check(capsys: pytest.CaptureFixture[str], head: str) -> tuple[int, str]:
    code = main(["check", "--repo", "example_app", "--base", BASE, "--head", head])
    return code, capsys.readouterr().out


def test_a_planted_unreachable_module_is_red_and_the_same_head_without_it_is_green(
    clone: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    clean = commit(clone, WIRED, "a module the handler imports")
    planted = commit(clone, PLANTED, "plant one module no root reaches")

    red_code, red = _check(capsys, planted)
    assert red_code == 1, red
    assert "UNREACHABLE-ADDED example_app src/example_app/planted.py" in red
    assert "flagged=1" in red
    assert "verdict=fail" in red

    green_code, green = _check(capsys, clean)
    assert green_code == 0, green
    assert "flagged=0" in green
    assert "verdict=pass" in green
    assert "UNREACHABLE-ADDED" not in green


def test_the_plant_is_the_only_difference_between_the_two_verdicts(
    clone: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    clean = commit(clone, WIRED, "wired")
    planted = commit(clone, PLANTED, "plant")
    flagged = [
        line
        for line in _check(capsys, planted)[1].splitlines()
        if line.startswith("UNREACHABLE-ADDED")
    ]
    assert flagged == ["UNREACHABLE-ADDED example_app src/example_app/planted.py"]
    assert clean != planted


def test_failing_control_dropped_contract_links_turn_a_wired_module_red(
    tmp_path: Path,
) -> None:
    repos = source("git", tmp_path)
    contract_wired = {
        "src/example_app/nodes/node_c/__init__.py": "",
        "src/example_app/nodes/node_c/contract.yaml": (
            "name: node_c\nhandler:\n  module: example_app.nodes.node_c.handler\n  class: Handler\n"
        ),
        "src/example_app/nodes/node_c/handler.py": "HANDLER = 1\n",
        "pyproject.toml": APP["pyproject.toml"].replace(
            'node_a = "example_app.nodes.node_a"',
            'node_a = "example_app.nodes.node_a"\nnode_c = "example_app.nodes.node_c"',
        ),
    }
    root = tmp_path / "example_app"
    commit(
        root,
        contract_wired,
        "a node registered by its entry point and linked by its contract",
    )
    wanted = GraphRules(roots=overlay().graph_roots)
    base = repos.snapshot("example_app", BASE, wanted.wanted)
    head = repos.snapshot("example_app", "HEAD", wanted.wanted)

    with_links = check_added(base, head, wanted)
    assert with_links.passed, with_links.flagged
    assert with_links.added >= 1

    dropped = GraphRules(roots=overlay().graph_roots, contract_links=False)
    broken = check_added(base, head, dropped)
    assert not broken.passed
    assert "src/example_app/nodes/node_c/handler.py" in broken.flagged
