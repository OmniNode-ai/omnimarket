# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-20915: the source-control nodes reach GitHub through their transport,
never through the ``gh`` CLI or a hand-written ``api.github.com`` URL.

The operator ruling of 2026-10-10T18:32:46Z moves every GitHub access onto the
source-control nodes so that ``gh`` can be turned off for lanes. A node that
shells out to ``gh`` re-opens the door the ruling closes: the lane that calls
the node then depends on a CLI the lane may not have, and the read bypasses the
node's quota and error posture.

The family is every node directory under ``src/omnimarket/nodes`` whose name
contains ``github``. It is found by name, so a new ``node_github_*`` node is
covered the day it lands, with no list to edit. The scan is on the AST, so a
docstring that names ``gh`` is not a violation and a quoted argv is.

Wired as the ``scm-nodes-no-gh-cli`` pre-commit hook and the ci.yml lint step
of the same name (the strict lint job gates CI Summary). The negative controls
below plant a violation in a throwaway family and require the scan to refuse
it, so a scan that went blind would fail here instead of passing empty.
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]
_NODES_ROOT = _REPO_ROOT / "src" / "omnimarket" / "nodes"
_REQUIRED_FAMILY = (
    "node_github_pr_poller_effect",
    "node_github_webhook_ingress_effect",
    "node_github_repo_gateway_effect",
    "node_github_repo_write_effect",
)
_CLI = "gh"
_API_HOST = "api.github.com"


def _is_cli_argv(node: ast.AST) -> bool:
    if not isinstance(node, ast.List | ast.Tuple) or not node.elts:
        return False
    first = node.elts[0]
    return isinstance(first, ast.Constant) and first.value == _CLI


_SPAWNERS = frozenset(
    {
        "run",
        "call",
        "check_call",
        "check_output",
        "Popen",
        "system",
        "popen",
        "create_subprocess_shell",
        "create_subprocess_exec",
    }
)


def _is_cli_shell_string(node: ast.AST) -> bool:
    """A process-spawning call whose first argument is a ``gh ...`` command line."""
    if not isinstance(node, ast.Call) or not node.args:
        return False
    func = node.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
    first = node.args[0]
    if name not in _SPAWNERS:
        return False
    if not isinstance(first, ast.Constant) or not isinstance(first.value, str):
        return False
    words = first.value.split(None, 1)
    return len(words) == 2 and words[0] == _CLI


def _is_cli_probe(node: ast.AST) -> bool:
    """``shutil.which("gh")``."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "which"
        and bool(node.args)
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == _CLI
    )


def _is_api_host(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and _API_HOST in node.value
    )


def scan_source(source: str, filename: str) -> list[str]:
    """Every ``gh`` CLI use or literal GitHub API host in one module's source.

    Docstrings are skipped: prose may name the CLI. Anything else is a use.
    """
    tree = ast.parse(source, filename=filename)
    docstrings = {
        id(body[0].value)
        for node in ast.walk(tree)
        if isinstance(
            node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
        )
        and (body := node.body)
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
    }
    found: list[str] = []
    for node in ast.walk(tree):
        if id(node) in docstrings:
            continue
        if _is_cli_argv(node):
            kind = "gh argv"
        elif _is_cli_shell_string(node):
            kind = "gh shell command"
        elif _is_cli_probe(node):
            kind = "gh probe"
        elif _is_api_host(node):
            kind = f"literal {_API_HOST}"
        else:
            continue
        found.append(f"{filename}:{node.lineno}: {kind}")
    return found


def family_nodes(nodes_root: Path) -> list[Path]:
    """Every node directory whose name contains ``github``."""
    return sorted(p for p in nodes_root.iterdir() if p.is_dir() and "github" in p.name)


def scan_family(nodes_root: Path) -> list[str]:
    """Violations across every ``*github*`` node directory under ``nodes_root``."""
    found: list[str] = []
    for node_dir in family_nodes(nodes_root):
        for path in sorted(node_dir.rglob("*.py")):
            if "tests" in path.relative_to(node_dir).parts:
                continue
            label = path.relative_to(nodes_root).as_posix()
            found.extend(scan_source(path.read_text(encoding="utf-8"), label))
    return found


_PLANTED = {
    "argv list": 'import subprocess\nsubprocess.run(["gh", "pr", "view", "1"])\n',
    "argv tuple": 'import subprocess\nsubprocess.run(("gh", "api", "/user"))\n',
    "shell string": 'import subprocess\nsubprocess.run("gh pr list", shell=True)\n',
    "which probe": 'import shutil\nGH = shutil.which("gh")\n',
    "literal host": 'URL = "https://api.github.com/repos/o/r"\n',
    "f-string host": 'def u(r):\n    return f"https://api.github.com/repos/{r}"\n',
}

_CLEAN = (
    '"""Reads PR state through the transport; no ``gh`` here."""\n'
    "from omnimarket.config.service_endpoints import GITHUB_REST_URL\n"
    "def url(repo: str) -> str:\n"
    '    return f"{GITHUB_REST_URL}/repos/{repo}"\n'
    'GH_LABEL = "ghost"\n'
    'GH_NOTE = "gh is a name, not a command"\n'
)


@pytest.mark.parametrize("label", sorted(_PLANTED))
def test_scan_refuses_planted_violation(label: str) -> None:
    assert scan_source(_PLANTED[label], "planted.py"), label


def test_scan_accepts_transport_use() -> None:
    assert scan_source(_CLEAN, "clean.py") == []


def test_scan_family_refuses_a_planted_node_and_ignores_other_nodes(
    tmp_path: Path,
) -> None:
    handlers = tmp_path / "node_github_planted_effect" / "handlers"
    handlers.mkdir(parents=True)
    (handlers / "handler_planted.py").write_text(_PLANTED["argv list"])
    other = tmp_path / "node_other_effect" / "handlers"
    other.mkdir(parents=True)
    (other / "handler_other.py").write_text(_PLANTED["argv list"])
    found = scan_family(tmp_path)
    assert len(found) == 1
    assert "node_github_planted_effect" in found[0]
    assert "node_other_effect" not in found[0]


def test_family_discovery_finds_the_moved_nodes() -> None:
    names = {p.name for p in family_nodes(_NODES_ROOT)}
    assert set(_REQUIRED_FAMILY) <= names


def test_source_control_nodes_do_not_use_the_gh_cli() -> None:
    assert scan_family(_NODES_ROOT) == []


def test_moved_nodes_are_registered_entry_points() -> None:
    pyproject = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text())
    registered = pyproject["project"]["entry-points"]["onex.nodes"]
    for name in _REQUIRED_FAMILY:
        assert registered.get(name) == f"omnimarket.nodes.{name}", name
        assert (_NODES_ROOT / name / "contract.yaml").is_file(), name


_SCAN_COMMAND = "pytest tests/ci/test_scm_nodes_no_gh_cli_omn20915.py"


def test_scan_is_wired_as_hook_and_lint_step() -> None:
    config = yaml.safe_load((_REPO_ROOT / ".pre-commit-config.yaml").read_text())
    hooks = {
        hook["id"]: hook for repo in config["repos"] for hook in repo.get("hooks", [])
    }
    assert _SCAN_COMMAND in hooks["scm-nodes-no-gh-cli"]["entry"]
    workflow = yaml.safe_load(
        (_REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text()
    )
    lint_runs = [step.get("run", "") for step in workflow["jobs"]["lint"]["steps"]]
    assert any(_SCAN_COMMAND in run for run in lint_runs)
