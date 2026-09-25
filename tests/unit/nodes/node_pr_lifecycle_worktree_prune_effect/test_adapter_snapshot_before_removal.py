# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""GitWorktreeAdapter.snapshot_before_removal (OMN-19539).

The adapter delegates to the shared helper
``$OMNI_HOME/omniclaude/scripts/worktree_removal_snapshot.py``. These tests
install a stand-in helper under a scratch ``OMNI_HOME`` that honours its
contract (exit 0 plus ``{"ok": true, "directory": ...}``, exit 3 on refusal);
the helper's own behaviour is tested in omniclaude.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_pr_lifecycle_worktree_prune_effect.handlers.adapter_git_worktree import (
    GitWorktreeAdapter,
)

pytestmark = pytest.mark.unit

_FAKE_HELPER = """import json, os, sys
wt, reason = sys.argv[1], sys.argv[3]
if os.environ.get("FAKE_SNAPSHOT_FAIL"):
    print(json.dumps({"ok": False, "error": "forced"})); sys.exit(3)
d = os.path.join(os.environ["OMNI_HOME"], ".onex_state", "worktree-removal-snapshots", os.path.basename(wt))
os.makedirs(d)
open(os.path.join(d, "reason.txt"), "w").write(reason)
print(json.dumps({"ok": True, "directory": d}))
"""


@pytest.fixture
def registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "registry"
    helper = home / "omniclaude" / "scripts" / "worktree_removal_snapshot.py"
    helper.parent.mkdir(parents=True)
    helper.write_text(_FAKE_HELPER, encoding="utf-8")
    monkeypatch.setenv("OMNI_HOME", str(home))
    monkeypatch.delenv("FAKE_SNAPSHOT_FAIL", raising=False)
    return home


def test_returns_the_snapshot_directory(registry: Path, tmp_path: Path) -> None:
    worktree = tmp_path / "OMN-1" / "omnimarket"
    worktree.mkdir(parents=True)

    directory = Path(GitWorktreeAdapter().snapshot_before_removal(str(worktree)))

    assert directory.is_dir()
    assert directory.parent == registry / ".onex_state" / "worktree-removal-snapshots"
    assert (directory / "reason.txt").read_text() == "pr_lifecycle_worktree_prune"


def test_a_refusing_helper_raises(
    registry: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_SNAPSHOT_FAIL", "1")
    with pytest.raises(RuntimeError, match="exit 3"):
        GitWorktreeAdapter().snapshot_before_removal(str(tmp_path))


def test_a_missing_helper_raises(registry: Path, tmp_path: Path) -> None:
    (registry / "omniclaude" / "scripts" / "worktree_removal_snapshot.py").unlink()
    with pytest.raises(RuntimeError, match="helper missing"):
        GitWorktreeAdapter().snapshot_before_removal(str(tmp_path))


def test_an_unset_registry_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OMNI_HOME", raising=False)
    with pytest.raises(RuntimeError, match="OMNI_HOME"):
        GitWorktreeAdapter().snapshot_before_removal(str(tmp_path))
