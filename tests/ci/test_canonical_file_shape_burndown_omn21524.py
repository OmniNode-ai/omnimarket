# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21524: the canonical-file-shape ratchet holds on a ticketed burn-down baseline.

The repository consumes the ``canonical-file-shape`` hook from omnibase_core at the
dev-merge tag its ``.pre-commit-config.yaml`` pins. Findings the whole-tree rules
report at the switch commit sit in ``.onex_ratchets/canonical_file_shape_burndown_baseline.txt``,
whose first comment line names the burn-down ticket. This test runs the entry the
hook publishes on throwaway trees:

* a listed handler no contract binds passes (positive control);
* a new unbound handler is refused as ``handler_outside_node``;
* a burn-down line added for an existing refused file is refused as ``baseline-growth``.

What each rule means is core's, not asserted here beyond its name.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
PRE_COMMIT_CONFIG = REPO_ROOT / ".pre-commit-config.yaml"
BURNDOWN = REPO_ROOT / ".onex_ratchets" / "canonical_file_shape_burndown_baseline.txt"

HOOK_ID = "canonical-file-shape"
TICKET = "OMN-21524"
BURNDOWN_HEADER = f"# Burn-down ticket: {TICKET}"
BASELINE_PATH = ".onex_ratchets/canonical_file_shape_baseline.txt"
BURNDOWN_PATH = ".onex_ratchets/canonical_file_shape_burndown_baseline.txt"
HANDLER_BODY = "def handle(request: object) -> object:\n    return request\n"


def _file_shape_block() -> dict[str, str]:
    config = yaml.safe_load(PRE_COMMIT_CONFIG.read_text())
    blocks = [
        repo
        for repo in config["repos"]
        if HOOK_ID in {hook["id"] for hook in repo["hooks"]}
    ]
    assert len(blocks) == 1, f"exactly one repo block consumes {HOOK_ID}"
    return {"repo": blocks[0]["repo"], "rev": blocks[0]["rev"]}


def _hook_args() -> list[str]:
    config = yaml.safe_load(PRE_COMMIT_CONFIG.read_text())
    for repo in config["repos"]:
        for hook in repo["hooks"]:
            if hook["id"] == HOOK_ID:
                return [str(arg) for arg in hook.get("args", [])]
    raise AssertionError(f"{HOOK_ID} is not wired")


@pytest.fixture(scope="module")
def pinned_core(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One shallow checkout of the core revision the committed config pins."""
    block = _file_shape_block()
    core = tmp_path_factory.mktemp("pinned_core") / "core"
    subprocess.run(
        [
            "git",
            "clone",
            "--quiet",
            "--depth",
            "1",
            "--branch",
            block["rev"],
            block["repo"],
            str(core),
        ],
        check=True,
        env=scrub_git_location_env(os.environ),
    )
    return core


def _run_ratchet(
    core: Path, tree: Path, *revisions: str
) -> subprocess.CompletedProcess[str]:
    hooks = yaml.safe_load((core / ".pre-commit-hooks.yaml").read_text())
    entry = shlex.split(next(h["entry"] for h in hooks if h["id"] == HOOK_ID))
    assert entry[:2] == ["python", "-m"], f"unexpected entry: {entry}"
    env = scrub_git_location_env(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(core / "src"), env.get("PYTHONPATH", "")) if part
    )
    return subprocess.run(
        [sys.executable, *entry[1:], *_hook_args(), *revisions],
        capture_output=True,
        text=True,
        cwd=tree,
        env=env,
        check=False,
    )


def _git(tree: Path, *args: str) -> str:
    done = subprocess.run(
        [
            "git",
            "-c",
            "user.name=canary",
            "-c",
            "user.email=canary@example.invalid",
            *args,
        ],
        cwd=tree,
        check=True,
        capture_output=True,
        text=True,
        env=scrub_git_location_env(os.environ),
    )
    return done.stdout.strip()


def _write(tree: Path, name: str, content: str) -> None:
    target = tree / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)


def _commit(tree: Path, message: str) -> str:
    _git(tree, "add", "-A")
    _git(tree, "commit", "-q", "-m", message)
    return _git(tree, "rev-parse", "HEAD")


def _tree_with_listed_handlers(tmp_path: Path, listed: list[str]) -> tuple[Path, str]:
    """A tree whose unbound handlers are all in the burn-down file; returns the base."""
    tree = tmp_path / "tree"
    tree.mkdir()
    _git(tree, "init", "-q")
    _write(tree, BASELINE_PATH, "# canonical-file-shape baseline\n")
    _write(
        tree,
        BURNDOWN_PATH,
        BURNDOWN_HEADER + " canary\n" + "".join(f"{name}\n" for name in listed),
    )
    for name in listed:
        _write(tree, name, HANDLER_BODY)
    _write(tree, "tests/test_canary.py", "def test_canary() -> None:\n    pass\n")
    return tree, _commit(tree, f"canary base ({TICKET})")


def test_committed_burndown_file_names_the_ticket_and_lists_only_paths() -> None:
    lines = BURNDOWN.read_text().splitlines()
    assert lines[0].startswith(BURNDOWN_HEADER)
    entries = [line for line in lines if not line.startswith("#")]
    assert entries
    assert len(entries) == len(set(entries))
    assert all((REPO_ROOT / entry).is_file() for entry in entries)


def test_listed_unbound_handler_passes_the_ratchet(
    tmp_path: Path, pinned_core: Path
) -> None:
    listed = "src/pkg/handlers/handler_listed.py"
    tree, _base = _tree_with_listed_handlers(tmp_path, [listed])

    result = _run_ratchet(pinned_core, tree, "--head", "HEAD")

    assert result.returncode == 0, result.stdout + result.stderr


def test_new_unbound_handler_is_refused_as_handler_outside_node(
    tmp_path: Path, pinned_core: Path
) -> None:
    listed = "src/pkg/handlers/handler_listed.py"
    added = "src/pkg/handlers/handler_added.py"
    tree, base = _tree_with_listed_handlers(tmp_path, [listed])
    _write(tree, added, HANDLER_BODY)
    _commit(tree, f"add a handler no contract binds ({TICKET})")

    result = _run_ratchet(pinned_core, tree, "--base", base, "--head", "HEAD")

    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert f"{added}: [handler_outside_node]" in output
    assert f"{listed}:" not in output


def test_burndown_line_added_for_an_existing_refused_file_is_refused_as_growth(
    tmp_path: Path, pinned_core: Path
) -> None:
    first = "src/pkg/handlers/handler_first.py"
    second = "src/pkg/handlers/handler_second.py"
    tree, _initial = _tree_with_listed_handlers(tmp_path, [first, second])
    header = BURNDOWN_HEADER + " canary\n"
    _write(tree, BURNDOWN_PATH, header + f"{first}\n")
    base = _commit(tree, f"drop one burn-down line ({TICKET})")
    _write(tree, BURNDOWN_PATH, header + f"{first}\n{second}\n")
    _commit(tree, f"add the line back ({TICKET})")

    result = _run_ratchet(pinned_core, tree, "--base", base, "--head", "HEAD")

    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert f"{BURNDOWN_PATH}: [baseline-growth]" in output, output
