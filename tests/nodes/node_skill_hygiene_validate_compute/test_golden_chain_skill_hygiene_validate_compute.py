# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Verdict parity of node_skill_hygiene_validate_compute with the change-control copy.

The oracle is onex_change_control's ``scripts/validation/validate_skill_hygiene.py``
at 1b0d2f03, kept byte-for-byte as test data (its git blob id is pinned below)
and run as the script it is. Each case builds a skill tree, runs the oracle and
the node's canonical entrypoint on it with the same flags, and asserts the same
exit status, the same text report and the same JSON report. Every rule family
has a defective tree the oracle itself rejects or reports, so no rule can drift
or be downgraded without a red here.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
from omnibase_core.enums.enum_severity import EnumSeverity

from omnimarket.nodes.node_skill_hygiene_validate_compute.handlers.handler_skill_hygiene_validate import (
    HandlerSkillHygieneValidate,
)
from omnimarket.nodes.node_skill_hygiene_validate_compute.models import (
    EnumSkillHygieneCheck,
    ModelSkillHygieneValidateRequest,
    ModelSkillHygieneValidateResult,
)
from omnimarket.nodes.node_skill_tree_read_effect.handlers.handler_skill_tree_read import (
    HandlerSkillTreeRead,
)
from omnimarket.nodes.node_skill_tree_read_effect.models import (
    ModelSkillTreeReadRequest,
)

pytestmark = pytest.mark.unit

_ORACLE = (
    Path(__file__).parent / "fixtures" / "occ_validate_skill_hygiene_1b0d2f03.py.oracle"
)
# git rev-parse 1b0d2f03:scripts/validation/validate_skill_hygiene.py in onex_change_control
_ORACLE_BLOB = "7fd889095006e1d7b6a3904b89175c8c26175d7e"
_READER_MODULE = "omnimarket.nodes.node_skill_tree_read_effect"
_NODE_MODULE = "omnimarket.nodes.node_skill_hygiene_validate_compute"


def _skill(root: Path, rel: str, *, name: str | None = None, extra: str = "") -> Path:
    skill_dir = root / rel
    skill_dir.mkdir(parents=True, exist_ok=True)
    declared = name if name is not None else skill_dir.name
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {declared}\ndescription: test skill\n{extra}---\n\n# {declared}\n",
        encoding="utf-8",
    )
    (skill_dir / "prompt.md").write_text("Run it.\n", encoding="utf-8")
    (skill_dir / "topics.yaml").write_text("topics: []\n", encoding="utf-8")
    return skill_dir


def _clean(root: Path) -> None:
    _skill(root, "alpha")
    _skill(root, "beta_gamma")
    _skill(root, "suite", extra="index: true\n")
    _skill(root, "suite/child_one")
    _skill(root, "suite/child_two")
    (root / "_lib").mkdir()
    (root / "_lib" / "helper.py").write_text("X = 1\n", encoding="utf-8")
    (root / "_shared" / "deep").mkdir(parents=True)
    (root / "_shared" / "deep" / "tool.py").write_text("Y = 2\n", encoding="utf-8")
    (root / "_shared" / "deep" / "topics.yaml").write_text("{}\n", encoding="utf-8")
    (root / "__init__.py").write_text("", encoding="utf-8")
    (root / "alpha" / "_impl").mkdir()
    (root / "alpha" / "_impl" / "run.py").write_text("Z = 3\n", encoding="utf-8")


def _underscore_names(root: Path) -> None:
    _clean(root)
    _skill(root, "bad-name")


def _underscore_names_nested(root: Path) -> None:
    _clean(root)
    _skill(root, "suite/child-three")


def _no_duplicate_names(root: Path) -> None:
    _clean(root)
    _skill(root, "beta-gamma", name="beta_gamma")


def _no_unindexed_nesting(root: Path) -> None:
    _clean(root)
    _skill(root, "family")
    _skill(root, "family/member")


def _no_unindexed_nesting_index_false(root: Path) -> None:
    _clean(root)
    _skill(root, "family", extra="index: false\n")
    _skill(root, "family/member")


def _skill_md_required(root: Path) -> None:
    _clean(root)
    (root / "bare").mkdir()
    (root / "bare" / "prompt.md").write_text("x\n", encoding="utf-8")


def _skill_md_required_nested(root: Path) -> None:
    _clean(root)
    (root / "suite" / "child_one" / "SKILL.md").unlink()
    (root / "suite" / "child_three").mkdir()
    (root / "suite" / "child_three" / "SKILL.md").write_text(
        "---\nname: child_three\n---\n", encoding="utf-8"
    )


def _name_matches_dir(root: Path) -> None:
    _clean(root)
    _skill(root, "delta", name="not_delta")


def _name_matches_dir_nested(root: Path) -> None:
    _clean(root)
    _skill(root, "suite/child_four", name="child-4")


def _no_python_in_skills(root: Path) -> None:
    _clean(root)
    (root / "alpha" / "run.py").write_text("print(1)\n", encoding="utf-8")


def _no_python_in_skills_nested(root: Path) -> None:
    _clean(root)
    (root / "suite" / "child_one" / "lib").mkdir()
    (root / "suite" / "child_one" / "lib" / "mod.py").write_text("", encoding="utf-8")


def _no_orphan_topics(root: Path) -> None:
    _clean(root)
    (root / "alpha" / "events").mkdir()
    (root / "alpha" / "events" / "topics.yaml").write_text("{}\n", encoding="utf-8")


# (case id, tree builder, strict, the oracle's exit status on it, the rule it exercises)
_CASES: list[
    tuple[str, Callable[[Path], None], bool, int, EnumSkillHygieneCheck | None]
] = [
    ("clean", _clean, False, 0, None),
    ("clean-strict", _clean, True, 0, None),
    (
        "underscore-names",
        _underscore_names,
        True,
        1,
        EnumSkillHygieneCheck.UNDERSCORE_NAMES,
    ),
    (
        "underscore-names-nested",
        _underscore_names_nested,
        False,
        1,
        EnumSkillHygieneCheck.UNDERSCORE_NAMES,
    ),
    (
        "no-duplicate-names",
        _no_duplicate_names,
        False,
        1,
        EnumSkillHygieneCheck.NO_DUPLICATE_NAMES,
    ),
    (
        "no-unindexed-nesting",
        _no_unindexed_nesting,
        False,
        1,
        EnumSkillHygieneCheck.NO_UNINDEXED_NESTING,
    ),
    (
        "no-unindexed-nesting-index-false",
        _no_unindexed_nesting_index_false,
        True,
        1,
        EnumSkillHygieneCheck.NO_UNINDEXED_NESTING,
    ),
    (
        "skill-md-required-migration",
        _skill_md_required,
        False,
        0,
        EnumSkillHygieneCheck.SKILL_MD_REQUIRED,
    ),
    (
        "skill-md-required-strict",
        _skill_md_required,
        True,
        1,
        EnumSkillHygieneCheck.SKILL_MD_REQUIRED,
    ),
    (
        "nested-dir-without-skill-md-strict",
        _skill_md_required_nested,
        True,
        0,
        EnumSkillHygieneCheck.NO_ORPHAN_TOPICS,
    ),
    (
        "name-matches-dir-migration",
        _name_matches_dir,
        False,
        0,
        EnumSkillHygieneCheck.NAME_MATCHES_DIR,
    ),
    (
        "name-matches-dir-strict",
        _name_matches_dir,
        True,
        1,
        EnumSkillHygieneCheck.NAME_MATCHES_DIR,
    ),
    (
        "name-matches-dir-nested-strict",
        _name_matches_dir_nested,
        True,
        1,
        EnumSkillHygieneCheck.NAME_MATCHES_DIR,
    ),
    (
        "no-python-in-skills",
        _no_python_in_skills,
        False,
        1,
        EnumSkillHygieneCheck.NO_PYTHON_IN_SKILLS,
    ),
    (
        "no-python-in-skills-nested",
        _no_python_in_skills_nested,
        True,
        1,
        EnumSkillHygieneCheck.NO_PYTHON_IN_SKILLS,
    ),
    (
        "no-orphan-topics",
        _no_orphan_topics,
        False,
        0,
        EnumSkillHygieneCheck.NO_ORPHAN_TOPICS,
    ),
    (
        "no-orphan-topics-strict",
        _no_orphan_topics,
        True,
        0,
        EnumSkillHygieneCheck.NO_ORPHAN_TOPICS,
    ),
]


def _run(argv: list[str], stdin: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, *argv],
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
    )


def _node(root: Path, flags: list[str]) -> subprocess.CompletedProcess[str]:
    """The node as wired: the tree reader's snapshot piped into the validator."""
    read = _run(["-m", _READER_MODULE, "--skills-root", str(root)])
    if read.returncode != 0:
        assert read.stdout == ""
    return _run(["-m", _NODE_MODULE, *flags], stdin=read.stdout)


def _both(
    root: Path, flags: list[str]
) -> tuple[subprocess.CompletedProcess[str], subprocess.CompletedProcess[str]]:
    oracle = _run([str(_ORACLE), "--skills-root", str(root), *flags])
    return oracle, _node(root, flags)


def _verdict(root: Path, strict: bool) -> ModelSkillHygieneValidateResult:
    tree = HandlerSkillTreeRead().handle(ModelSkillTreeReadRequest(skills_root=root))
    return HandlerSkillHygieneValidate().handle(
        ModelSkillHygieneValidateRequest(tree=tree, strict=strict)
    )


def test_oracle_is_the_pinned_change_control_copy() -> None:
    data = _ORACLE.read_bytes()
    blob = hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()
    assert blob == _ORACLE_BLOB


@pytest.mark.parametrize(
    ("builder", "strict", "oracle_status", "rule"),
    [case[1:] for case in _CASES],
    ids=[case[0] for case in _CASES],
)
def test_node_verdict_equals_oracle(
    tmp_path: Path,
    builder: Callable[[Path], None],
    strict: bool,
    oracle_status: int,
    rule: EnumSkillHygieneCheck | None,
) -> None:
    root = tmp_path / "skills"
    root.mkdir()
    builder(root)
    flags = ["--strict"] if strict else []

    oracle_json, node_json = _both(root, [*flags, "--json"])
    assert oracle_json.returncode == oracle_status, oracle_json.stderr
    assert node_json.returncode == oracle_json.returncode, node_json.stderr
    oracle_report = json.loads(oracle_json.stdout)
    assert json.loads(node_json.stdout) == oracle_report

    oracle_text, node_text = _both(root, flags)
    assert (node_text.returncode, node_text.stdout) == (
        oracle_text.returncode,
        oracle_text.stdout,
    )

    reported = {v["check"] for v in oracle_report["violations"]}
    if rule is None:
        assert reported == set()
    else:
        assert rule.value in reported

    verdict = _verdict(root, strict)
    assert verdict.passed is (oracle_status == 0)
    assert verdict.skills_root == str(root)
    assert verdict.error_count == oracle_report["error_count"]
    assert verdict.warning_count == oracle_report["warning_count"]
    assert [
        (v.path, v.check.value, v.severity.value.upper(), v.message)
        for v in verdict.violations
    ] == [
        (v["path"], v["check"], v["severity"], v["message"])
        for v in oracle_report["violations"]
    ]


@pytest.mark.parametrize("strict", [False, True])
def test_missing_root_is_a_usage_error_like_the_oracle(
    tmp_path: Path, strict: bool
) -> None:
    flags = ["--strict"] if strict else []
    oracle, node = _both(tmp_path / "absent", flags)
    assert oracle.returncode == 2
    assert node.returncode == 2
    assert node.stdout == oracle.stdout == ""


def test_strict_promotes_only_the_two_migration_rules(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    root.mkdir()
    _skill_md_required(root)
    _skill(root, "delta", name="not_delta")
    (root / "alpha" / "events").mkdir()
    (root / "alpha" / "events" / "topics.yaml").write_text("{}\n", encoding="utf-8")
    loose = _verdict(root, False)
    strict = _verdict(root, True)
    severity = {v.check: v.severity for v in strict.violations}
    assert severity[EnumSkillHygieneCheck.SKILL_MD_REQUIRED] is EnumSeverity.ERROR
    assert severity[EnumSkillHygieneCheck.NAME_MATCHES_DIR] is EnumSeverity.ERROR
    assert severity[EnumSkillHygieneCheck.NO_ORPHAN_TOPICS] is EnumSeverity.WARNING
    assert loose.passed
    assert {v.severity for v in loose.violations} == {EnumSeverity.WARNING}
    assert not strict.passed
