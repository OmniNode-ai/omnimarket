# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The publisher's loader refuses owner entries from any source other than overlays main, naming the source (OMN-23388).

On 2026-11-09 the publisher switched to loading overlays main while owner tickets added since 12:50Z sat only on a side
branch, so every red row read nobody on it. When the lab overlay declares an ``owner_source``, the floor overlay file
(which carries ``causes.owners``) must be byte-equal to the same file at that ref of its own git checkout; a checkout on a
side branch with other owner entries, an uncommitted edit and a file outside any checkout are each refused, and the
refusal names the source. The control is the same cycle on a checkout equal to main, which runs.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

from omnimarket.handlers.emission_sink import InMemoryEmissionSink
from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.models.floor_17a import (
    ModelFloorSimpleTickRequest,
    ModelOwnerSourceOverlay,
)
from omnimarket.nodes.node_floor_simple_effect.handlers.owner_source import (
    check_owner_source,
)
from tests.nodes.node_floor_simple_effect.support import OVERLAYS, build_home, environ
from tests.nodes.node_floor_simple_effect.test_cycle_exits_floor_simple import (
    cycle_phases,
    handler,
)

pytestmark = pytest.mark.unit

SPEC = ModelOwnerSourceOverlay(remote_ref="origin/main")
FLOOR = "node_floor_simple_compute/overlay.yaml"


def git(root: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=True,
        env=scrub_git_location_env(os.environ),
    )
    return done.stdout.strip()


def checkout(tmp_path: Path, *, declare: bool = True) -> Path:
    """A git checkout of the overlay fixtures whose main commit is origin/main."""
    root = tmp_path / "overlays"
    shutil.copytree(OVERLAYS, root)
    if declare:
        lab = root / "node_floor_simple_effect" / "overlay.yaml"
        lab.write_text(
            lab.read_text(encoding="utf-8")
            + "\nowner_source:\n  remote_ref: origin/main\n",
            encoding="utf-8",
        )
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "t@example.invalid")
    git(root, "config", "user.name", "t")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "main")
    git(root, "update-ref", "refs/remotes/origin/main", "HEAD")
    return root


def add_owner_on_side_branch(root: Path) -> None:
    git(root, "checkout", "-q", "-b", "side-owners")
    path = root / FLOOR
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    doc["causes"]["owners"] = {
        **doc["causes"].get("owners", {}),
        "stoppage:new": ["OMN-9"],
    }
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    git(root, "commit", "-q", "-am", "owner on a side branch")


def env_for(home: Path, root: Path) -> dict[str, str]:
    return {**environ(home), "ONEX_SKILL_OVERLAY_ROOTS": str(root)}


def test_golden_a_checkout_equal_to_main_is_accepted(tmp_path: Path) -> None:
    root = checkout(tmp_path)
    check_owner_source(root / FLOOR, SPEC)


def test_error_a_side_branch_owner_map_is_refused_naming_the_source(
    tmp_path: Path,
) -> None:
    root = checkout(tmp_path)
    add_owner_on_side_branch(root)
    with pytest.raises(NodeOverlayError) as refused:
        check_owner_source(root / FLOOR, SPEC)
    text = str(refused.value)
    assert "side-owners" in text
    assert "origin/main" in text
    assert str(root / FLOOR) in text


def test_error_an_uncommitted_owner_edit_on_main_is_refused(tmp_path: Path) -> None:
    root = checkout(tmp_path)
    path = root / FLOOR
    path.write_text(path.read_text(encoding="utf-8") + "\n# edited\n", encoding="utf-8")
    with pytest.raises(NodeOverlayError, match="main"):
        check_owner_source(path, SPEC)


def test_error_an_overlay_outside_any_checkout_is_refused(tmp_path: Path) -> None:
    loose = tmp_path / "loose" / FLOOR
    loose.parent.mkdir(parents=True)
    shutil.copy(OVERLAYS / FLOOR, loose)
    with pytest.raises(NodeOverlayError, match="not in a git checkout"):
        check_owner_source(loose, SPEC)


def test_error_a_ref_that_does_not_exist_is_refused(tmp_path: Path) -> None:
    root = checkout(tmp_path)
    with pytest.raises(NodeOverlayError, match="origin/nope"):
        check_owner_source(
            root / FLOOR, ModelOwnerSourceOverlay(remote_ref="origin/nope")
        )


def test_golden_the_cycle_runs_on_a_checkout_equal_to_main(tmp_path: Path) -> None:
    home = build_home(tmp_path)
    root = checkout(tmp_path)
    sink = InMemoryEmissionSink()
    result = handler(sink).run(
        ModelFloorSimpleTickRequest(home=str(home)), environ=env_for(home, root)
    )
    assert result.ok
    assert cycle_phases(sink)[-1] == ("success", None)


def test_error_the_cycle_refuses_a_side_branch_owner_map_and_closes_its_start(
    tmp_path: Path,
) -> None:
    home = build_home(tmp_path)
    root = checkout(tmp_path)
    add_owner_on_side_branch(root)
    sink = InMemoryEmissionSink()
    with pytest.raises(NodeOverlayError, match="side-owners"):
        handler(sink).run(
            ModelFloorSimpleTickRequest(home=str(home)), environ=env_for(home, root)
        )
    assert cycle_phases(sink) == [("start", None), ("failure", "overlay_refused")]
    assert "side-owners" in sink.events[-1].failure_detail


def test_golden_a_lab_overlay_that_declares_no_owner_source_checks_nothing(
    tmp_path: Path,
) -> None:
    home = build_home(tmp_path)
    root = checkout(tmp_path, declare=False)
    add_owner_on_side_branch(root)
    result = handler(InMemoryEmissionSink()).run(
        ModelFloorSimpleTickRequest(home=str(home)), environ=env_for(home, root)
    )
    assert result.ok
