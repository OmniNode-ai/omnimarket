# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-20914: the table the gh shim refuses from stays readable and served.

Ruling 2026-10-10T18:32:46Z: GitHub access moves onto the source-control nodes and the gh
shim refuses a gh write from a lane once ``onex scm`` serves it. The shim (omniclaude,
``scripts/user-bin/gh``, lane write guard) does not hold its own list. It reads this
repository's installed copy:

* the ``operations`` block of ``node_github_repo_write_effect/contract.yaml``, one awk pass
  that takes an operation only when its one-line description holds ``replaces gh <group>
  <sub>.`` (a final period included);
* ``handlers/scm/handler_scm_dispatch.py``, two literal greps: ``verb="<group>-<sub>"`` and
  ``replaces="gh <group> <sub>"``.

A reworded description, a renamed verb or a reformatted literal here therefore turns a
refusal off in every lane with no failure anywhere. ``shim_coverage_violations`` reads both
files the way the shim does and names every native command the shim would stop refusing.
API writes, outside the native-command shim, must name an explicit HTTP mutation and an
exactly matching served SCM verb. The cases below plant violations in both paths. Run by the required lint job
and by a pre-commit hook; ``test_the_lint_job_and_the_hook_run_this_check`` fails when
either is dropped.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.handlers.scm.handler_scm_dispatch import SCM_VERBS
from omnimarket.models.scm import EnumScmNode, ModelScmVerb

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]
_CONTRACT = _ROOT / "src/omnimarket/nodes/node_github_repo_write_effect/contract.yaml"
_DISPATCH = _ROOT / "src/omnimarket/handlers/scm/handler_scm_dispatch.py"
_THIS = "tests/ci/test_gh_shim_coverage_table_omn20914.py"
_BINDING_TESTS = (
    "tests/unit/handlers/scm/test_onex_scm_command.py::test_every_contract_operation_has_exactly_one_verb",
    "tests/unit/handlers/scm/test_onex_scm_command.py::test_no_refused_verb_is_also_served",
)
_HOOK_ID = "gh-shim-coverage-table"
# The shim's awk pattern, verbatim: ``replaces gh [a-z]+ [a-z]+\.``.
_SHIM_CLAUSE = re.compile(r"replaces gh ([a-z]+) ([a-z]+)\.")
# gh api writes are outside the native-command shim's scope. Their replacements
# must still be served exactly once and match a one-line, explicit HTTP request.
_API_CLAUSE = re.compile(
    r"  - name: ([a-z_]+)\n    description: [^\n]*replaces "
    r'(gh api --method (?:POST|PATCH|PUT|DELETE) /repos/[^\s".]+)\.'
)


def _shim_table(contract_text: str) -> dict[str, str]:
    """operation name -> "<group> <sub>" for what the shim's awk pass takes.

    The shim walks the top-level ``operations:`` block line by line: a ``- name:`` line sets
    the current operation and the next ``description:`` line, on one line, is searched for
    the clause. A description folded over several lines is never seen.
    """
    table: dict[str, str] = {}
    section = ""
    op = ""
    for line in contract_text.splitlines():
        if line and not line[0].isspace() and line[0] != "#":
            section = line.split()[0]
        if section != "operations:":
            continue
        stripped = line.strip()
        if re.match(r"-\s*name:", stripped):
            op = re.sub(r'["\s]', "", stripped.split(":", 1)[1])
        elif stripped.startswith("description:") and op:
            found = _SHIM_CLAUSE.search(line)
            if found:
                table[op] = f"{found.group(1)} {found.group(2)}"
            op = ""
    return table


def shim_coverage_violations(
    contract_text: str, dispatch_text: str, verbs: Sequence[ModelScmVerb]
) -> list[str]:
    """Every way the gh shim would silently stop refusing a write ``onex scm`` serves."""
    violations: list[str] = []
    declared: Any = yaml.safe_load(contract_text).get("operations") or []
    declared_names = [str(entry["name"]) for entry in declared]
    table = _shim_table(contract_text)
    api_table = dict(_API_CLAUSE.findall(contract_text))
    for name in declared_names:
        if name not in table:
            replacement = api_table.get(name)
            served = [
                v for v in verbs if v.node is EnumScmNode.WRITE and v.operation == name
            ]
            if replacement is not None:
                if len(served) != 1 or served[0].replaces != replacement:
                    violations.append(
                        f"operation {name}: API replacement is not served exactly once as {replacement!r}"
                    )
                elif any(
                    literal not in dispatch_text
                    for literal in (
                        f'verb="{served[0].verb}"',
                        f'replaces="{replacement}"',
                    )
                ):
                    violations.append(
                        f"operation {name}: API replacement literals are missing from the dispatch table"
                    )
                continue
            violations.append(
                f"operation {name}: its description carries no one-line "
                "'replaces gh <group> <sub>.' clause, so the shim never refuses it"
            )
    by_gh: dict[str, list[str]] = {}
    for name, gh in table.items():
        by_gh.setdefault(gh, []).append(name)
        group, sub = gh.split(" ")
        for literal in (f'verb="{group}-{sub}"', f'replaces="gh {gh}"'):
            if literal not in dispatch_text:
                violations.append(
                    f"operation {name}: handler_scm_dispatch.py holds no literal "
                    f"{literal}, so the shim's grep for gh {gh} fails"
                )
        served = [
            v
            for v in verbs
            if v.node is EnumScmNode.WRITE and v.verb == f"{group}-{sub}"
        ]
        if len(served) != 1:
            violations.append(
                f"operation {name}: {len(served)} write verbs named {group}-{sub}; "
                "the shim names exactly one"
            )
        elif served[0].operation != name or served[0].replaces != f"gh {gh}":
            violations.append(
                f"operation {name}: verb {group}-{sub} serves operation "
                f"{served[0].operation} replacing {served[0].replaces!r}, "
                f"not {name} replacing 'gh {gh}'"
            )
    for gh, names in by_gh.items():
        if len(names) > 1:
            violations.append(
                f"gh {gh} is claimed by {', '.join(sorted(names))}; the shim names one"
            )
    return violations


def _real() -> tuple[str, str]:
    return _CONTRACT.read_text("utf-8"), _DISPATCH.read_text("utf-8")


def test_the_shim_reads_every_write_operation_the_repo_serves() -> None:
    contract_text, dispatch_text = _real()
    assert shim_coverage_violations(contract_text, dispatch_text, SCM_VERBS) == []
    # Positive control: all six native CLI writes remain covered by the shim.
    assert sorted(_shim_table(contract_text)) == [
        "pr_close",
        "pr_comment",
        "pr_create",
        "pr_edit",
        "pr_ready",
        "workflow_dispatch",
    ]


def _plant_in_contract(old: str, new: str) -> tuple[str, str, Sequence[ModelScmVerb]]:
    contract_text, dispatch_text = _real()
    assert old in contract_text
    return contract_text.replace(old, new), dispatch_text, SCM_VERBS


def _plant_in_dispatch(old: str, new: str) -> tuple[str, str, Sequence[ModelScmVerb]]:
    contract_text, dispatch_text = _real()
    assert old in dispatch_text
    return contract_text, dispatch_text.replace(old, new), SCM_VERBS


def _plant_extra_operation() -> tuple[str, str, Sequence[ModelScmVerb]]:
    contract_text, dispatch_text = _real()
    anchor = "\nmodes:\n"
    assert anchor in contract_text
    extra = (
        "  - name: label_add\n"
        '    description: "Request ModelRepoWriteRequest; adds a label."\n'
        "    transport: rest\n"
    )
    return (
        contract_text.replace(anchor, "\n" + extra + anchor, 1),
        dispatch_text,
        SCM_VERBS,
    )


def _plant_wrong_operation() -> tuple[str, str, Sequence[ModelScmVerb]]:
    contract_text, dispatch_text = _real()
    swapped = [
        v.model_copy(update={"operation": "pr_ready"}) if v.verb == "pr-close" else v
        for v in SCM_VERBS
    ]
    return contract_text, dispatch_text, swapped


def _plant_duplicate_claim() -> tuple[str, str, Sequence[ModelScmVerb]]:
    return _plant_in_contract("replaces gh pr comment.", "replaces gh pr create.")


_PLANTED = {
    "description loses the final period": lambda: _plant_in_contract(
        "replaces gh pr create.", "replaces gh pr create"
    ),
    "description hyphenates the gh verb": lambda: _plant_in_contract(
        "replaces gh pr edit.", "replaces gh pr-edit."
    ),
    "verb renamed in the dispatch table": lambda: _plant_in_dispatch(
        'verb="pr-edit"', 'verb="pr-update"'
    ),
    "replaces literal reformatted": lambda: _plant_in_dispatch(
        'replaces="gh pr ready"', 'replaces = "gh pr ready"'
    ),
    "operation added with no clause": _plant_extra_operation,
    "verb bound to another operation": _plant_wrong_operation,
    "two operations claim one gh verb": _plant_duplicate_claim,
}


@pytest.mark.parametrize("case", sorted(_PLANTED))
def test_a_planted_violation_is_refused(case: str) -> None:
    contract_text, dispatch_text, verbs = _PLANTED[case]()
    assert shim_coverage_violations(contract_text, dispatch_text, verbs), case


def test_a_description_folded_over_two_lines_is_refused() -> None:
    contract_text, dispatch_text = _real()
    old = 'description: "Request ModelRepoWriteRequest, result ModelRepoWriteCompleted or ModelRepoWriteFailed; replaces gh pr ready.'
    assert old in contract_text
    folded = contract_text.replace(
        old,
        'description: "Request ModelRepoWriteRequest, result ModelRepoWriteCompleted or ModelRepoWriteFailed;\n      replaces gh pr ready.',
    )
    violations = shim_coverage_violations(folded, dispatch_text, SCM_VERBS)
    assert any("pr_ready" in v for v in violations)


def _hook() -> dict[str, Any]:
    config = yaml.safe_load((_ROOT / ".pre-commit-config.yaml").read_text("utf-8"))
    hooks = [
        hook
        for repo in config["repos"]
        for hook in repo["hooks"]
        if hook["id"] == _HOOK_ID
    ]
    assert len(hooks) == 1
    hook: dict[str, Any] = hooks[0]
    return hook


@pytest.mark.parametrize("target", [_THIS, *_BINDING_TESTS])
def test_the_lint_job_and_the_hook_run_this_check(target: str) -> None:
    hook = _hook()
    assert target in hook["entry"]
    assert hook["entry"].startswith("uv run pytest ")
    assert hook["pass_filenames"] is False
    assert "pre-commit" in hook["stages"]
    workflow = yaml.safe_load(
        (_ROOT / ".github" / "workflows" / "ci.yml").read_text("utf-8")
    )
    runs = [str(step.get("run", "")) for step in workflow["jobs"]["lint"]["steps"]]
    assert any(target in run and "pytest" in run for run in runs)


@pytest.mark.parametrize(
    "watched",
    [
        "src/omnimarket/nodes/node_github_repo_write_effect/contract.yaml",
        "src/omnimarket/handlers/scm/handler_scm_dispatch.py",
        _THIS,
    ],
)
def test_the_hook_files_filter_selects_what_the_shim_reads(watched: str) -> None:
    assert re.search(_hook()["files"], watched)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("replaces gh api --method POST", "replaces gh api --method GET"),
        ("replaces gh api --method POST", "calls gh api --method POST"),
        ('verb="check-run-create"', 'verb="check-run-post"'),
        ('replaces="gh api --method POST', 'replaces="gh api --method PATCH'),
    ],
)
def test_api_write_replacement_drift_is_refused(old: str, new: str) -> None:
    contract_text, dispatch_text = _real()
    assert old in contract_text or old in dispatch_text
    assert shim_coverage_violations(
        contract_text.replace(old, new), dispatch_text.replace(old, new), SCM_VERBS
    )


def test_api_write_requires_exactly_one_served_operation() -> None:
    contract_text, dispatch_text = _real()
    removed = [spec for spec in SCM_VERBS if spec.operation != "check_run_create"]
    assert shim_coverage_violations(contract_text, dispatch_text, removed)
    spec = next(spec for spec in SCM_VERBS if spec.operation == "check_run_create")
    assert shim_coverage_violations(contract_text, dispatch_text, [*SCM_VERBS, spec])
