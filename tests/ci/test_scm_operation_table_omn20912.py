# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-20912: the table that turns a lane's gh write into an ``onex scm`` verb stays whole.

RULING 2026-10-10T18:32:46Z: GitHub access moves onto the source-control nodes and
gh CLI access is turned off for sub-agents. The gh shim refuses a lane's gh write only
when ``onex scm`` serves its replacement, and it reads that from two places in this
repository: the ``operations`` block of node_github_repo_write_effect's contract.yaml
(each description names the gh verb it replaces as ``replaces gh <verb>.``) and the
``SCM_VERBS`` table of handlers/scm/handler_scm_dispatch.py. Drop an operation, edit a
description or point a verb at another operation here and the shim silently stops
refusing that gh verb: gh is back on for lanes and nothing is red.

``table_violations`` is the check. This file runs as a step of the lint job (a required
context) and as a check-only pre-commit hook, and its planted-violation cases prove the
check refuses each way the table can go quiet.
"""

from __future__ import annotations

import ast
import copy
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]
_WRITE_CONTRACT = (
    _ROOT / "src/omnimarket/nodes/node_github_repo_write_effect/contract.yaml"
)
_DISPATCH = _ROOT / "src/omnimarket/handlers/scm/handler_scm_dispatch.py"
_HOOK_ID = "scm-operation-table"
_THIS_TEST = "tests/ci/test_scm_operation_table_omn20912.py"

#: The gh writes a lane makes (the plan's slice 1) and the operation that replaces each.
LANE_WRITES: dict[str, str] = {
    "pr create": "pr_create",
    "pr edit": "pr_edit",
    "pr ready": "pr_ready",
    "pr close": "pr_close",
    "pr comment": "pr_comment",
    "workflow run": "workflow_dispatch",
    "api --method POST /repos/{o}/{r}/check-runs": "check_run_create",
}
_REGISTRY_OWNER = "OmniNode-ai"


def _dispatch_write_verbs(source: str) -> dict[str, str]:
    """operation -> ``replaces`` of every ``ModelScmVerb(... node=_W ...)`` in the source."""
    found: dict[str, str] = {}
    for call in ast.walk(ast.parse(source)):
        if not (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == "ModelScmVerb"
        ):
            continue
        kwargs = {kw.arg: kw.value for kw in call.keywords}
        node = kwargs.get("node")
        if not (isinstance(node, ast.Name) and node.id == "_W"):
            continue
        operation, replaces = kwargs.get("operation"), kwargs.get("replaces")
        if isinstance(operation, ast.Constant) and isinstance(replaces, ast.Constant):
            found[str(operation.value)] = str(replaces.value)
    return found


def table_violations(contract: Mapping[str, Any], dispatch_source: str) -> list[str]:
    """Every way the write table stops naming a gh verb to the shim; empty when whole."""
    violations: list[str] = []
    operations = {op["name"]: op for op in contract.get("operations") or []}
    served = _dispatch_write_verbs(dispatch_source)
    for gh_verb, name in LANE_WRITES.items():
        op = operations.get(name)
        if op is None:
            violations.append(f"contract has no operation {name} for gh {gh_verb}")
        elif f"replaces gh {gh_verb}." not in str(op.get("description", "")):
            violations.append(
                f"operation {name} does not say 'replaces gh {gh_verb}.' in its description"
            )
        if name not in served:
            violations.append(f"SCM_VERBS has no write verb for operation {name}")
        elif served[name] != f"gh {gh_verb}":
            violations.append(
                f"SCM_VERBS replaces {served[name]!r} for {name}, not 'gh {gh_verb}'"
            )
    for name in sorted(set(operations) - set(LANE_WRITES.values())):
        violations.append(f"operation {name} replaces no gh write a lane makes")
    for name in sorted(set(served) - set(operations)):
        violations.append(
            f"SCM_VERBS serves {name}, which the contract does not declare"
        )
    floor = (contract.get("quota_floor") or {}).get("min_remaining") or {}
    for resource in ("core", "graphql"):
        if not isinstance(floor.get(resource), int) or floor[resource] <= 0:
            violations.append(f"quota_floor.min_remaining.{resource} is not positive")
    owners = (contract.get("write_config") or {}).get("github_mode_by_owner") or {}
    if owners.get(_REGISTRY_OWNER) != "enforce":
        violations.append(
            f"write_config puts {_REGISTRY_OWNER} in {owners.get(_REGISTRY_OWNER)!r}, "
            "not enforce: lanes lose gh and onex scm sends nothing"
        )
    return violations


def _tree() -> tuple[dict[str, Any], str]:
    return yaml.safe_load(_WRITE_CONTRACT.read_text("utf-8")), _DISPATCH.read_text(
        "utf-8"
    )


def test_the_tree_table_is_whole() -> None:
    contract, source = _tree()
    assert table_violations(contract, source) == []


def _drop_operation(contract: dict[str, Any], name: str) -> None:
    contract["operations"] = [o for o in contract["operations"] if o["name"] != name]


def _reword_description(contract: dict[str, Any], name: str) -> None:
    for op in contract["operations"]:
        if op["name"] == name:
            op["description"] = op["description"].replace(
                "replaces gh", "supersedes gh"
            )


def _add_operation(contract: dict[str, Any], name: str) -> None:
    contract["operations"].append(
        {"name": name, "description": "replaces gh pr merge.", "transport": "rest"}
    )


def _zero_quota(contract: dict[str, Any], resource: str) -> None:
    contract["quota_floor"]["min_remaining"][resource] = 0


def _dry_run_owner(contract: dict[str, Any], owner: str) -> None:
    contract["write_config"]["github_mode_by_owner"][owner] = "dry_run"


# (planted violation applied to the contract, argument, the violation the check names)
_CONTRACT_PLANTS = [
    pytest.param(_drop_operation, "pr_edit", "no operation pr_edit", id="op-dropped"),
    pytest.param(
        _reword_description,
        "pr_close",
        "operation pr_close does not say 'replaces gh pr close.'",
        id="description-reworded",
    ),
    pytest.param(
        _add_operation, "pr_merge", "pr_merge replaces no gh write", id="op-added"
    ),
    pytest.param(
        _zero_quota, "graphql", "quota_floor.min_remaining.graphql", id="quota-zero"
    ),
    pytest.param(_dry_run_owner, _REGISTRY_OWNER, "not enforce", id="owner-dry-run"),
    pytest.param(
        _drop_operation,
        "check_run_create",
        "no operation check_run_create",
        id="check-run-dropped",
    ),
    pytest.param(
        _reword_description,
        "check_run_create",
        "operation check_run_create does not say",
        id="check-run-description-reworded",
    ),
]


@pytest.mark.parametrize(("plant", "arg", "expected"), _CONTRACT_PLANTS)
def test_check_refuses_a_planted_contract_violation(
    plant: Any, arg: str, expected: str
) -> None:
    contract, source = _tree()
    planted = copy.deepcopy(contract)
    plant(planted, arg)

    violations = table_violations(planted, source)

    assert any(expected in v for v in violations), violations
    assert table_violations(contract, source) == [], "the clean tree is the control"


_DISPATCH_PLANTS = [
    pytest.param(
        'operation="pr_edit"',
        'operation="pr_amend"',
        "SCM_VERBS has no write verb for operation pr_edit",
        id="verb-points-at-another-operation",
    ),
    pytest.param(
        'replaces="gh pr ready"',
        'replaces="gh pr mark-ready"',
        "not 'gh pr ready'",
        id="replaces-edited",
    ),
]


@pytest.mark.parametrize(("old", "new", "expected"), _DISPATCH_PLANTS)
def test_check_refuses_a_planted_dispatch_violation(
    old: str, new: str, expected: str
) -> None:
    contract, source = _tree()
    assert source.count(old) == 1, "the planted text must match the tree exactly once"

    violations = table_violations(contract, source.replace(old, new))

    assert any(expected in v for v in violations), violations


def test_check_refuses_a_write_verb_removed_from_the_table() -> None:
    contract, source = _tree()
    removed = re.sub(
        r'    ModelScmVerb\(\n        verb="pr-comment".*?\n    \),\n',
        "",
        source,
        count=1,
        flags=re.DOTALL,
    )
    assert removed != source

    violations = table_violations(contract, removed)

    assert any(
        "SCM_VERBS has no write verb for operation pr_comment" in v for v in violations
    ), violations


def _hook() -> dict[str, Any]:
    config = yaml.safe_load((_ROOT / ".pre-commit-config.yaml").read_text("utf-8"))
    hooks = [
        hook
        for repo in config["repos"]
        for hook in repo["hooks"]
        if hook["id"] == _HOOK_ID
    ]
    assert len(hooks) == 1
    return hooks[0]


def test_scm_operation_table_is_wired_into_pre_commit_check_only() -> None:
    hook = _hook()
    assert _THIS_TEST in hook["entry"]
    assert hook["entry"].startswith("uv run pytest ")
    assert hook["pass_filenames"] is False
    assert "pre-commit" in hook["stages"]
    for watched in (
        _THIS_TEST,
        "src/omnimarket/nodes/node_github_repo_write_effect/contract.yaml",
        "src/omnimarket/handlers/scm/handler_scm_dispatch.py",
    ):
        assert re.search(hook["files"], watched), watched


def test_scm_operation_table_is_wired_into_the_required_lint_job() -> None:
    workflow = yaml.safe_load(
        (_ROOT / ".github" / "workflows" / "ci.yml").read_text("utf-8")
    )
    runs = [str(step.get("run", "")) for step in workflow["jobs"]["lint"]["steps"]]
    assert any(_THIS_TEST in run and "pytest" in run for run in runs)
