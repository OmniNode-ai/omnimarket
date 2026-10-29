# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21496 T2.6: the consumed omnibase_core commit hooks refuse and write nothing.

The commit hooks of this repository are the check-only ids that omnibase_core
publishes (``check-hook-writes``, ``commit-hygiene``, ``commit-hygiene-message``).
A file without a final newline, a staged ``tmp/`` file and a commit message with
an AI co-author trailer are each refused and every byte of the tree is the same
after each run. What a hook id means is core's rule, not asserted here.

Two routes prove it, both with the repository pin the committed
``.pre-commit-config.yaml`` carries for the ids:

* ``pre-commit`` itself runs the hooks on a throwaway tree. It builds the hook
  environment, so it is slow on a cold cache.
* the entry each hook publishes in ``.pre-commit-hooks.yaml`` runs from a shallow
  checkout of the pinned core revision. It needs no hook environment, so the
  hosted evidence check of the contract binds this route.
"""

from __future__ import annotations

import hashlib
import os
import shlex
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
PRE_COMMIT_CONFIG = REPO_ROOT / ".pre-commit-config.yaml"

HOOK_WRITES_ID = "check-hook-writes"
PRE_COMMIT_STAGE_ID = "commit-hygiene"
COMMIT_MSG_STAGE_ID = "commit-hygiene-message"

MESSAGE_FILE = "COMMIT_EDITMSG"
MESSAGE_WITH_TRAILER = (
    b"fix: a change\n\nCo-Authored-By: Claude <noreply@anthropic.com>\n"
)
REFUSED_FILES = [
    ("notes.txt", b"no final newline", "missing_final_newline"),
    ("tmp/scratch.txt", b"scratch\n", "staged_tmp_file"),
]

RunFilesStage = Callable[[list[str]], "subprocess.CompletedProcess[str]"]
RunMessageStage = Callable[[], "subprocess.CompletedProcess[str]"]


def _consumed_core_block(*hook_ids: str) -> dict[str, Any]:
    """The committed repo block that consumes the given core hook ids."""
    config = yaml.safe_load(PRE_COMMIT_CONFIG.read_text())
    blocks = [
        repo
        for repo in config["repos"]
        if set(hook_ids) <= {hook["id"] for hook in repo["hooks"]}
    ]
    assert len(blocks) == 1, f"exactly one repo block consumes {sorted(hook_ids)}"
    return {"repo": blocks[0]["repo"], "rev": blocks[0]["rev"]}


def _tree_digest(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file() and ".git" not in path.relative_to(root).parts
    }


def _git(tree: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=tree,
        check=True,
        env=scrub_git_location_env(os.environ),
    )


def _new_tree(tmp_path: Path) -> Path:
    tree = tmp_path / "tree"
    tree.mkdir()
    _git(tree, "init", "-q")
    return tree


def _stage(tree: Path, name: str, content: bytes) -> None:
    target = tree / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    _git(tree, "add", name)


def _unstage(tree: Path, name: str) -> None:
    _git(tree, "rm", "-q", "--cached", name)


def _assert_refuses_and_writes_nothing(
    tree: Path, run_files: RunFilesStage, run_message: RunMessageStage
) -> None:
    # positive control: a clean staged file passes, so a refusal below is the rule's
    _stage(tree, "clean.txt", b"one line\n")
    result = run_files(["clean.txt"])
    assert result.returncode == 0, result.stdout + result.stderr
    _unstage(tree, "clean.txt")

    for name, content, rule in REFUSED_FILES:
        _stage(tree, name, content)
        before = _tree_digest(tree)
        result = run_files([name])
        assert result.returncode != 0, result.stdout + result.stderr
        assert rule in result.stdout + result.stderr
        assert _tree_digest(tree) == before
        _unstage(tree, name)

    (tree / MESSAGE_FILE).write_bytes(MESSAGE_WITH_TRAILER)
    before = _tree_digest(tree)
    result = run_message()
    assert result.returncode != 0, result.stdout + result.stderr
    assert "ai_coauthor_trailer" in result.stdout + result.stderr
    assert _tree_digest(tree) == before


def _core_checkout(parent: Path, block: dict[str, Any]) -> Path:
    """A shallow checkout of the core revision the committed config pins."""
    core = parent / "core"
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


def _published_entry(core: Path, hook_id: str) -> list[str]:
    """The argv prefix core publishes as the entry of a hook id."""
    hooks = yaml.safe_load((core / ".pre-commit-hooks.yaml").read_text())
    entry = shlex.split(next(hook["entry"] for hook in hooks if hook["id"] == hook_id))
    assert entry[:2] == ["python", "-m"], f"unexpected entry for {hook_id}: {entry}"
    return [sys.executable, *entry[1:]]


def _run_entry(
    core: Path, hook_id: str, files: list[str], cwd: Path
) -> subprocess.CompletedProcess[str]:
    env = scrub_git_location_env(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(core / "src"), env.get("PYTHONPATH", "")) if part
    )
    return subprocess.run(
        [*_published_entry(core, hook_id), *files],
        capture_output=True,
        text=True,
        cwd=cwd,
        env=env,
        check=False,
    )


def test_consumed_commit_hooks_refuse_and_leave_the_tree_byte_identical(
    tmp_path: Path,
) -> None:
    tree = _new_tree(tmp_path)
    config = tmp_path / "config.yaml"
    block = _consumed_core_block(PRE_COMMIT_STAGE_ID, COMMIT_MSG_STAGE_ID)
    block["hooks"] = [{"id": PRE_COMMIT_STAGE_ID}, {"id": COMMIT_MSG_STAGE_ID}]
    config.write_text(yaml.safe_dump({"repos": [block]}))

    def run(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "pre_commit", "run", *args, "--config", str(config)],
            capture_output=True,
            text=True,
            cwd=tree,
            env=scrub_git_location_env(os.environ),
            check=False,
        )

    _assert_refuses_and_writes_nothing(
        tree,
        lambda _files: run(PRE_COMMIT_STAGE_ID),
        lambda: run(
            COMMIT_MSG_STAGE_ID,
            "--hook-stage",
            "commit-msg",
            "--commit-msg-filename",
            MESSAGE_FILE,
        ),
    )


@pytest.fixture(scope="module")
def pinned_core(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One shallow checkout of the pinned core revision for the entry route."""
    return _core_checkout(
        tmp_path_factory.mktemp("pinned_core"),
        _consumed_core_block(HOOK_WRITES_ID, PRE_COMMIT_STAGE_ID, COMMIT_MSG_STAGE_ID),
    )


def test_published_hook_entries_refuse_and_leave_the_tree_byte_identical(
    tmp_path: Path, pinned_core: Path
) -> None:
    tree = _new_tree(tmp_path)
    core = pinned_core

    _assert_refuses_and_writes_nothing(
        tree,
        lambda files: _run_entry(core, PRE_COMMIT_STAGE_ID, files, tree),
        lambda: _run_entry(core, COMMIT_MSG_STAGE_ID, [MESSAGE_FILE], tree),
    )


def test_published_hook_writes_entry_passes_the_committed_config_and_refuses_a_rewriter(
    tmp_path: Path, pinned_core: Path
) -> None:
    core = pinned_core

    committed = _run_entry(
        core, HOOK_WRITES_ID, [PRE_COMMIT_CONFIG.name], PRE_COMMIT_CONFIG.parent
    )
    assert committed.returncode == 0, committed.stdout + committed.stderr

    # positive control: the same entry refuses a config whose hooks rewrite files
    rewriter = tmp_path / "rewriter"
    rewriter.mkdir()
    (rewriter / ".pre-commit-config.yaml").write_text(
        yaml.safe_dump(
            {
                "repos": [
                    {
                        "repo": "https://github.com/pre-commit/pre-commit-hooks",
                        "rev": "v6.0.0",
                        "hooks": [{"id": "end-of-file-fixer"}],
                    },
                    {
                        "repo": "local",
                        "hooks": [
                            {
                                "id": "strip-trailer",
                                "name": "strip trailer",
                                "entry": "perl -i -ne 'print unless /^co-authored-by:/i'",
                                "language": "system",
                            }
                        ],
                    },
                ]
            }
        )
    )
    before = _tree_digest(rewriter)
    refused = _run_entry(core, HOOK_WRITES_ID, [".pre-commit-config.yaml"], rewriter)
    assert refused.returncode != 0, refused.stdout + refused.stderr
    assert "hook_writes_id_end_of_file_fixer" in refused.stdout + refused.stderr
    assert "hook_writes_in_place_edit" in refused.stdout + refused.stderr
    assert _tree_digest(rewriter) == before
