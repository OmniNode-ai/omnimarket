# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lookup as an agent runs it: the node's own CLI on real git clones (OMN-24847).

Before the lookup verb the CLI refused ``lookup`` as an invalid choice, so an agent had no command
that asks the graph for importers and fell back to grep.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_code_graph_effect import runtime_code_graph
from omnimarket.nodes.node_code_graph_effect.runtime_code_graph import main
from tests.nodes.node_code_graph_effect.code_graph_support import (
    APP,
    CORE,
    EXAMPLE,
    make_clone,
)

pytestmark = pytest.mark.unit


def _install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, lookup: bool = True
) -> None:
    clones = tmp_path / "clones"
    make_clone(clones, "example_core", CORE)
    make_clone(clones, "example_app", APP)
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    document["graph_backend"] = "git"
    if not lookup:
        del document["lookup"]
    target = tmp_path / "overlays" / "node_code_graph_effect" / "overlay.yaml"
    target.parent.mkdir(parents=True)
    target.write_text(yaml.safe_dump(document), encoding="utf-8")
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path / "overlays"))
    monkeypatch.setenv("EXAMPLE_REPOS_ROOT", str(clones))
    runtime_code_graph._VERSIONS.clear()


def _importers(capsys: pytest.CaptureFixture[str], path: str) -> tuple[int, str]:
    code = main(
        ["lookup", "--question", "importers", "--repo", "example_core", "--path", path]
    )
    return code, capsys.readouterr().out


def test_the_lookup_verb_returns_the_importers_of_a_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install(tmp_path, monkeypatch)
    code, out = _importers(capsys, "src/example_core/models.py")
    assert code == 0, out
    assert "MATCH src/example_core/cli.py\n" in out
    summary = out.splitlines()[-1]
    assert summary.startswith(
        "LOOKUP question=importers repo=example_core status=found answered_from="
    )
    assert " matches=0 " not in summary


def test_a_file_nothing_imports_is_a_clean_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install(tmp_path, monkeypatch)
    code, out = _importers(capsys, "src/example_core/unused.py")
    assert code == 0, out
    assert "MATCH" not in out
    assert "status=not-found" in out
    assert "matches=0" in out


def test_the_definition_question_names_the_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install(tmp_path, monkeypatch)
    code = main(
        [
            "lookup",
            "--question",
            "definition",
            "--repo",
            "example_core",
            "--symbol",
            "Thing",
        ]
    )
    out = capsys.readouterr().out
    assert code == 0, out
    assert "MATCH src/example_core/models.py name=Thing line=1 detail=class\n" in out


def test_an_overlay_with_no_lookup_block_exits_one_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install(tmp_path, monkeypatch, lookup=False)
    code, out = _importers(capsys, "src/example_core/models.py")
    assert code == 1, out
    assert "status=failed" in out
    assert "reason=the overlay has no lookup block" in out


def test_an_unset_repos_root_is_indeterminate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install(tmp_path, monkeypatch)
    monkeypatch.delenv("EXAMPLE_REPOS_ROOT")
    code = main(
        [
            "lookup",
            "--question",
            "importers",
            "--repo",
            "example_core",
            "--path",
            "x.py",
        ]
    )
    assert code == 3
    assert "EXAMPLE_REPOS_ROOT is not set" in capsys.readouterr().err


def test_a_question_without_its_argument_is_indeterminate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install(tmp_path, monkeypatch)
    code = main(["lookup", "--question", "importers", "--repo", "example_core"])
    assert code == 3
    assert "the importers question lacks its argument" in capsys.readouterr().err
