# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The daily install canary reads a fresh head and the drill triggers the same probe on demand (OMN-22955)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

from omnimarket.nodes.node_install_canary_effect.handlers.handler_install_canary import (
    HandlerInstallCanary,
    ModelInstallCanaryRequest,
    ModelInstallProbe,
)

pytestmark = pytest.mark.unit

PREFIX = "plugins/omni/skills"
ROOT_DATA = "skills/merge-drain/scripts/landing_floors.json"
TREE = {
    "plugins/omni/skills/merge-drain/SKILL.md": "# merge-drain\n",
    "plugins/omni/skills/merge-drain/scripts/landing_controller.py": "# controller\n",
    "plugins/omni/skills/merge-drain/scripts/landing_floors.json": '{"floor": 1}\n',
    "plugins/omni/scripts/landing_policy.py": "# policy\n",
}


def git(repo: Path, *args: str) -> str:
    identity = {
        "GIT_AUTHOR_NAME": "lane",
        "GIT_AUTHOR_EMAIL": "lane@example.invalid",
        "GIT_COMMITTER_NAME": "lane",
        "GIT_COMMITTER_EMAIL": "lane@example.invalid",
    }
    done = subprocess.run(
        ["git", "-C", str(repo), "-c", "commit.gpgsign=false", *args],
        capture_output=True,
        text=True,
        check=True,
        env={**scrub_git_location_env(os.environ), **identity},
    )
    return done.stdout.strip()


def write(root: Path, rel: str, body: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


@pytest.fixture
def clone(tmp_path: Path) -> Path:
    repo = tmp_path / "clone"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    for rel, body in TREE.items():
        write(repo, rel, body)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    return repo


@pytest.fixture
def host_copy(tmp_path: Path) -> Path:
    root = tmp_path / "host-copy"
    for rel, body in TREE.items():
        write(root, rel.removeprefix("plugins/omni/"), body)
    return root


def _no_refresh(path: Path, ref: str) -> None:
    """A refresh that has nothing to fetch: the fixture clone already holds ``origin/main``."""


def advance(clone: Path, rel: str) -> str:
    """Merge one more file of the plugin to origin/main, as the canonical-clone sync does."""
    write(clone, f"{PREFIX}/{rel}", f"# {rel}\n")
    git(clone, "add", "-A")
    git(clone, "commit", "-q", "-m", f"add {rel}")
    head = git(clone, "rev-parse", "HEAD")
    git(clone, "update-ref", "refs/remotes/origin/main", head)
    return head


def test_daily_and_drill_run_the_same_probe_and_the_drill_flips_on_the_lost_file(
    clone: Path, host_copy: Path
) -> None:
    handler = HandlerInstallCanary({"refresh_clone": _no_refresh})
    head = git(clone, "rev-parse", "origin/main")

    daily = handler.daily_install_probe(clone, "origin/main", host_copy, PREFIX)
    assert (daily.trigger, daily.head, daily.missing, daily.ok) == (
        "daily",
        head,
        (),
        True,
    )

    drill = handler.drill_install_probe(
        clone, "origin/main", host_copy, PREFIX, delete=ROOT_DATA
    )
    assert (drill.trigger, drill.head, drill.fault) == ("drill", head, ROOT_DATA)
    assert drill.missing == (ROOT_DATA,)
    assert drill.ok is False
    # The drill ran on a scratch copy: the install it was triggered against is untouched, and its next daily run passes.
    assert (host_copy / ROOT_DATA).is_file()
    assert handler.daily_install_probe(clone, "origin/main", host_copy, PREFIX).ok


def test_daily_and_drill_the_daily_probe_reads_a_fresh_head_each_run(
    clone: Path, host_copy: Path
) -> None:
    refreshed: list[tuple[Path, str]] = []

    def refresh_clone(path: Path, ref: str) -> None:
        refreshed.append((path, ref))
        advance(path, f"merge-sweep/new_{len(refreshed)}.json")

    handler = HandlerInstallCanary({"refresh_clone": refresh_clone})
    first = handler.daily_install_probe(clone, "origin/main", host_copy, PREFIX)
    second = handler.daily_install_probe(clone, "origin/main", host_copy, PREFIX)

    assert refreshed == [(clone, "origin/main")] * 2
    assert first.head != second.head
    assert second.head == git(clone, "rev-parse", "origin/main")
    # The copy was installed from the first head: the fresh head's new root data file is what the probe finds missing.
    assert first.missing == ("skills/merge-sweep/new_1.json",)
    assert second.missing == (
        "skills/merge-sweep/new_1.json",
        "skills/merge-sweep/new_2.json",
    )
    assert not second.ok


def test_daily_and_drill_the_typed_boundary_dispatches_both_triggers(
    clone: Path, host_copy: Path
) -> None:
    handler = HandlerInstallCanary({"refresh_clone": _no_refresh})
    daily = handler.handle(
        ModelInstallCanaryRequest(
            "daily_install_probe", (clone, "origin/main", host_copy, PREFIX)
        )
    ).value
    drill = handler.handle(
        ModelInstallCanaryRequest(
            "drill_install_probe",
            (clone, "origin/main", host_copy, PREFIX),
            {"delete": ROOT_DATA},
        )
    ).value
    assert isinstance(daily, ModelInstallProbe)
    assert (daily.trigger, drill.trigger) == ("daily", "drill")
    assert daily.head == drill.head


def test_daily_and_drill_refuse_a_trigger_nobody_declared_and_a_fault_the_copy_lacks(
    clone: Path, host_copy: Path
) -> None:
    handler = HandlerInstallCanary({})
    with pytest.raises(ValueError, match="unknown install probe trigger"):
        handler.install_probe("hourly", clone, "origin/main", host_copy, PREFIX)
    with pytest.raises(ValueError, match="is no file of"):
        handler.drill_install_probe(
            clone, "origin/main", host_copy, PREFIX, delete="skills/nope.json"
        )


def test_drill_refuses_a_fault_that_points_outside_the_install(
    clone: Path, host_copy: Path, tmp_path: Path
) -> None:
    """A fault path that reaches past the install must not delete the live file it names (review finding)."""
    handler = HandlerInstallCanary({})
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    elsewhere = tmp_path / "linked"
    elsewhere.symlink_to(outside.parent, target_is_directory=True)
    (host_copy / "skills" / "escape").symlink_to(
        outside.parent, target_is_directory=True
    )
    live = host_copy / ROOT_DATA

    for fault in (
        str(live),
        str(outside),
        "../../outside.json",
        "skills/escape/outside.json",
    ):
        with pytest.raises(ValueError, match="is no file of"):
            handler.drill_install_probe(
                clone, "origin/main", host_copy, PREFIX, delete=fault
            )
        assert live.is_file()
        assert outside.is_file()


def test_a_probe_with_no_refresh_clone_is_refused_not_read_from_a_stale_local_ref(
    clone: Path, host_copy: Path
) -> None:
    """OMN-22955 review finding: without ``refresh_clone`` the probe silently read the local ref."""
    handler = HandlerInstallCanary({})
    with pytest.raises(ValueError, match="refresh_clone"):
        handler.daily_install_probe(clone, "origin/main", host_copy, PREFIX)
    with pytest.raises(ValueError, match="refresh_clone"):
        handler.drill_install_probe(
            clone, "origin/main", host_copy, PREFIX, delete=ROOT_DATA
        )
