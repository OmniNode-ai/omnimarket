# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_skill_tree_read_effect reads exactly what the skill validators judge, in their order."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from omnimarket.models.skill_tree import ModelSkillTreeSnapshot
from omnimarket.nodes.node_skill_tree_read_effect.handlers.handler_skill_tree_read import (
    HandlerSkillTreeRead,
)
from omnimarket.nodes.node_skill_tree_read_effect.models import (
    ModelSkillTreeReadRequest,
)

pytestmark = pytest.mark.unit

_READER_MODULE = "omnimarket.nodes.node_skill_tree_read_effect"


def _tree(root: Path) -> None:
    (root / "b_skill").mkdir(parents=True)
    (root / "b_skill" / "SKILL.md").write_text("---\nname: b_skill\n---\n", "utf-8")
    (root / "b_skill" / "prompt.md").write_text("run\n", "utf-8")
    (root / "b_skill" / "topics.yaml").write_text("{}\n", "utf-8")
    (root / "b_skill" / "child").mkdir()
    (root / "b_skill" / "child" / "SKILL.md").write_text("child\n", "utf-8")
    (root / "b_skill" / "notes.txt").write_text("x\n", "utf-8")
    (root / "a-skill" / "deep").mkdir(parents=True)
    (root / "a-skill" / "deep" / "topics.yaml").write_text("{}\n", "utf-8")
    (root / "a-skill" / "x.py").write_text("", "utf-8")
    (root / "a" / "y.py").mkdir(parents=True)
    (root / "README.md").write_text("readme\n", "utf-8")


def test_snapshot_records_what_the_validators_read(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    _tree(root)
    snapshot = HandlerSkillTreeRead().handle(
        ModelSkillTreeReadRequest(skills_root=root)
    )

    assert snapshot.skills_root == str(root)
    assert [e.name for e in snapshot.entries] == [
        "README.md",
        "a",
        "a-skill",
        "b_skill",
    ]
    readme, _, dashed, skill = snapshot.entries
    assert not readme.is_dir
    assert readme.children == ()
    assert not dashed.has_skill_md
    assert dashed.skill_md_text is None
    assert skill.has_skill_md
    assert skill.has_prompt_md
    assert skill.skill_md_text == "---\nname: b_skill\n---\n"
    assert skill.prompt_md_text == "run\n"
    assert [
        (c.name, c.is_dir, c.has_skill_md, c.skill_md_text) for c in skill.children
    ] == [
        ("SKILL.md", False, False, None),
        ("child", True, True, "child\n"),
        ("notes.txt", False, False, None),
        ("prompt.md", False, False, None),
        ("topics.yaml", False, False, None),
    ]
    # Sorted as Path objects, so "a/..." sorts before "a-skill/...".
    assert snapshot.python_files == ("a/y.py", "a-skill/x.py")
    assert [(t.path, t.parent_has_skill_md) for t in snapshot.topics_files] == [
        ("a-skill/deep/topics.yaml", False),
        ("b_skill/topics.yaml", True),
    ]
    assert [t.path for t in snapshot.topics_files] == [
        str(p.relative_to(root)) for p in sorted(root.rglob("topics.yaml"))
    ]


def test_entrypoint_writes_the_snapshot_and_refuses_a_missing_root(
    tmp_path: Path,
) -> None:
    root = tmp_path / "skills"
    _tree(root)
    ok = subprocess.run(
        [sys.executable, "-m", _READER_MODULE, "--skills-root", str(root)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert ok.returncode == 0, ok.stderr
    assert ModelSkillTreeSnapshot.model_validate_json(ok.stdout) == (
        HandlerSkillTreeRead().handle(ModelSkillTreeReadRequest(skills_root=root))
    )

    missing = subprocess.run(
        [sys.executable, "-m", _READER_MODULE, "--skills-root", str(tmp_path / "no")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert missing.returncode == 2
    assert missing.stdout == ""
    with pytest.raises(NotADirectoryError):
        HandlerSkillTreeRead().handle(
            ModelSkillTreeReadRequest(skills_root=tmp_path / "no")
        )
