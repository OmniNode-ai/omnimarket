# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Verdict parity of node_skill_contract_validate_compute with the change-control copy.

The oracle is onex_change_control's ``scripts/validation/validate_skill_contracts.py``
at 47342e56 (the revision omniclaude's lock pins), kept byte-for-byte as test
data (its git blob id is pinned below) and run as the script it is. Each case
builds a skill tree, runs the oracle and the node's canonical entrypoint on it
with the same flags, and asserts the same exit status, the same text report and
the same JSON report. Every rule family has a defective tree the oracle itself
rejects or reports, so no rule can drift or be downgraded without a red here.
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

from omnimarket.nodes.node_skill_contract_validate_compute.handlers.handler_skill_contract_validate import (
    HandlerSkillContractValidate,
)
from omnimarket.nodes.node_skill_contract_validate_compute.models import (
    EnumSkillContractCheck,
    ModelSkillContractValidateRequest,
    ModelSkillContractValidateResult,
)
from omnimarket.nodes.node_skill_tree_read_effect.handlers.handler_skill_tree_read import (
    HandlerSkillTreeRead,
)
from omnimarket.nodes.node_skill_tree_read_effect.models import (
    ModelSkillTreeReadRequest,
)

pytestmark = pytest.mark.unit

_ORACLE = (
    Path(__file__).parent
    / "fixtures"
    / "occ_validate_skill_contracts_47342e56.py.oracle"
)
# git rev-parse 47342e56:scripts/validation/validate_skill_contracts.py in onex_change_control
_ORACLE_BLOB = "21ac9548a95ea27e6594147ef081f12cf5ca6a06"
_READER_MODULE = "omnimarket.nodes.node_skill_tree_read_effect"
_NODE_MODULE = "omnimarket.nodes.node_skill_contract_validate_compute"


def _skill(
    root: Path,
    name: str,
    *,
    frontmatter: str = "",
    body: str = "",
    prompt: str | None = "Run it.\n",
) -> Path:
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: test skill\n{frontmatter}---\n\n# {name}\n{body}",
        encoding="utf-8",
    )
    if prompt is not None:
        (skill_dir / "prompt.md").write_text(prompt, encoding="utf-8")
    return skill_dir


_RUNNER_ARGS = "args:\n  - name: --dry-run\n    description: plan only\n  - --repo (required): target repo\n  - name: pr_number\n"
_RUNNER_BODY = "The status is `ready_to_merge` once the result is `merged_clean`.\nPlain `ok` and `untracked_word` stay unchecked.\n"
_RUNNER_PROMPT = (
    "Parse --dry-run and the repo argument.\n"
    'Then Skill(skill="onex:target_skill", args="--dry-run --repo x") and /onex:target-skill.\n'
    "Emit ready-to-merge, or merged_clean.\n"
)


def _clean(root: Path) -> None:
    _skill(
        root,
        "runner",
        frontmatter=_RUNNER_ARGS,
        body=_RUNNER_BODY,
        prompt=_RUNNER_PROMPT,
    )
    _skill(
        root,
        "target_skill",
        frontmatter="args:\n  - name: --dry-run\n  - name: --repo\n",
        prompt="Use --dry-run and --repo.\n",
    )
    _skill(root, "no_prompt_no_args", prompt=None)
    (root / "_lib").mkdir()
    (root / "_lib" / "prompt.md").write_text("/onex:nowhere\n", encoding="utf-8")
    (root / "not_a_dir.md").write_text("/onex:nowhere\n", encoding="utf-8")


def _args_parity(root: Path) -> None:
    _clean(root)
    _skill(
        root,
        "gamma",
        frontmatter="args:\n  - name: --since-date\n",
        prompt="No flags here.\n",
    )


def _args_parity_no_prompt(root: Path) -> None:
    _clean(root)
    _skill(root, "gamma", frontmatter="args:\n  - name: --since-date\n", prompt=None)


def _sub_skill_exists(root: Path) -> None:
    _clean(root)
    _skill(
        root,
        "gamma",
        prompt='Call Skill(skill="onex:missing_skill") then /onex:also-missing.\n',
    )


def _sub_skill_exists_dir_without_skill_md(root: Path) -> None:
    _clean(root)
    (root / "hollow").mkdir()
    _skill(root, "gamma", prompt="Call /onex:hollow.\n")


def _sub_skill_args(root: Path) -> None:
    _clean(root)
    _skill(
        root, "gamma", prompt='Skill(skill="onex:target_skill", args="--bogus-flag")\n'
    )


def _duplicate_frontmatter(root: Path) -> None:
    _clean(root)
    _skill(root, "gamma", frontmatter="description: twice\n")


def _spec_prompt_predicates(root: Path) -> None:
    _clean(root)
    _skill(
        root,
        "gamma",
        body="On success the output is `never_emitted` and the state `half-done`.\n",
        prompt="Nothing relevant.\n",
    )


# (case id, tree builder, strict, the oracle's exit status on it, the rule it exercises)
_CASES: list[
    tuple[str, Callable[[Path], None], bool, int, EnumSkillContractCheck | None]
] = [
    ("clean", _clean, False, 0, None),
    ("clean-strict", _clean, True, 0, None),
    ("args-parity", _args_parity, False, 1, EnumSkillContractCheck.ARGS_PARITY),
    (
        "args-parity-no-prompt",
        _args_parity_no_prompt,
        False,
        0,
        EnumSkillContractCheck.ARGS_PARITY,
    ),
    (
        "args-parity-no-prompt-strict",
        _args_parity_no_prompt,
        True,
        0,
        EnumSkillContractCheck.ARGS_PARITY,
    ),
    (
        "sub-skill-exists",
        _sub_skill_exists,
        False,
        1,
        EnumSkillContractCheck.SUB_SKILL_EXISTS,
    ),
    (
        "sub-skill-exists-hollow-dir",
        _sub_skill_exists_dir_without_skill_md,
        True,
        1,
        EnumSkillContractCheck.SUB_SKILL_EXISTS,
    ),
    (
        "sub-skill-args",
        _sub_skill_args,
        False,
        0,
        EnumSkillContractCheck.SUB_SKILL_ARGS,
    ),
    (
        "sub-skill-args-strict",
        _sub_skill_args,
        True,
        0,
        EnumSkillContractCheck.SUB_SKILL_ARGS,
    ),
    (
        "duplicate-frontmatter",
        _duplicate_frontmatter,
        False,
        1,
        EnumSkillContractCheck.DUPLICATE_FRONTMATTER,
    ),
    (
        "spec-prompt-predicates",
        _spec_prompt_predicates,
        False,
        0,
        EnumSkillContractCheck.SPEC_PROMPT_PREDICATES,
    ),
    (
        "spec-prompt-predicates-strict",
        _spec_prompt_predicates,
        True,
        1,
        EnumSkillContractCheck.SPEC_PROMPT_PREDICATES,
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


def _verdict(root: Path, strict: bool) -> ModelSkillContractValidateResult:
    tree = HandlerSkillTreeRead().handle(ModelSkillTreeReadRequest(skills_root=root))
    return HandlerSkillContractValidate().handle(
        ModelSkillContractValidateRequest(tree=tree, strict=strict)
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
    rule: EnumSkillContractCheck | None,
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


def test_strict_promotes_only_spec_prompt_predicates(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    root.mkdir()
    _spec_prompt_predicates(root)
    _skill(root, "delta", frontmatter="args:\n  - name: --since-date\n", prompt=None)
    _skill(
        root,
        "epsilon",
        prompt='Skill(skill="onex:target_skill", args="--bogus-flag")\n',
    )
    loose = _verdict(root, False)
    strict = _verdict(root, True)
    severity = {v.check: v.severity for v in strict.violations}
    assert severity[EnumSkillContractCheck.SPEC_PROMPT_PREDICATES] is EnumSeverity.ERROR
    assert severity[EnumSkillContractCheck.ARGS_PARITY] is EnumSeverity.WARNING
    assert severity[EnumSkillContractCheck.SUB_SKILL_ARGS] is EnumSeverity.WARNING
    assert loose.passed
    assert {v.severity for v in loose.violations} == {EnumSeverity.WARNING}
    assert not strict.passed
