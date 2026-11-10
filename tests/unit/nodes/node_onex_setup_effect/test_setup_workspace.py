# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Real Git failure controls, specified before the proven-workspace adapter.

Catch: dirty/local-only work lost by checkout, one clone changed before a later
refusal, foreign remotes or folders adopted, stale markers masking HEAD drift,
untrusted pin strings executed, and the deliberately excluded Claude pin used.
These local-repository controls do not prove customer network installation.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_workspace import (
    WORKSPACE_REPOSITORIES,
    HandlerSetupWorkspace,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_plan import (
    ModelSetupPlan,
)

pytestmark = pytest.mark.unit


def git(root: Path, *args: str, stdin: str | None = None, year: int = 2020) -> str:
    return subprocess.run(
        ("git", "-C", str(root), *args),
        input=stdin,
        text=True,
        capture_output=True,
        check=True,
        timeout=20,
        env={
            **scrub_git_location_env(os.environ),
            "GIT_AUTHOR_NAME": "Setup fixture",
            "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
            "GIT_COMMITTER_NAME": "Setup fixture",
            "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
            "GIT_AUTHOR_DATE": f"{year}-01-01T00:00:00+00:00",
            "GIT_COMMITTER_DATE": f"{year}-01-01T00:00:00+00:00",
        },
    ).stdout.strip()


def commit(root: Path, year: int, parent: str | None = None) -> str:
    git(root, "add", ".")
    tree = git(root, "write-tree")
    sha = git(
        root,
        "commit-tree",
        tree,
        *(["-p", parent] if parent else []),
        stdin="Workspace fixture\n",
        year=year,
    )
    git(root, "update-ref", "refs/heads/main", sha)
    return sha


@pytest.fixture
def stack(
    tmp_path: Path,
) -> tuple[ModelSetupPlan, HandlerSetupWorkspace, dict[str, str]]:
    remotes = tmp_path / "origins"
    remotes.mkdir()
    workspace = tmp_path / "workspace with spaces"
    workspace.mkdir()
    (workspace / ".onex-setup-workspace").write_text("onex setup\n")
    targets: dict[str, str] = {}
    for name in WORKSPACE_REPOSITORIES:
        source = remotes / name
        source.mkdir()
        git(source, "init", "--initial-branch=main")
        (source / "payload").write_text("proven\n")
        targets[name] = commit(source, 2020)
    infra = remotes / "omnibase_infra"
    (infra / ".github").mkdir()
    # The lab installer explicitly excludes the Claude sibling pin. Its
    # as-of-anchor default-branch commit must win over this invalid pin.
    pins = {**targets, "omniclaude": "f" * 40}
    (infra / ".github" / "sibling-pins.yaml").write_text(yaml.safe_dump({"pins": pins}))
    anchor = commit(infra, 2021, targets["omnibase_infra"])
    targets["omnibase_infra"] = anchor
    for name in WORKSPACE_REPOSITORIES:
        source = remotes / name
        (source / "payload").write_text("newer than proven\n")
        commit(source, 2022, targets[name])
    handler = HandlerSetupWorkspace(
        anchor=anchor, remote_url=lambda name: str(remotes / name)
    )
    return ModelSetupPlan(workspace=workspace, developer=True), handler, targets


def heads(plan: ModelSetupPlan) -> dict[str, str]:
    return {
        name: git(plan.workspace / name, "rev-parse", "HEAD")
        for name in WORKSPACE_REPOSITORIES
    }


def test_package_install_does_not_acquire_source_clones(tmp_path: Path) -> None:
    workspace = tmp_path / "package workspace"
    workspace.mkdir()
    handler = HandlerSetupWorkspace()
    plan = ModelSetupPlan(workspace=workspace)
    assert handler.observe(plan).status == "skipped"
    handler.apply(plan)
    assert list(workspace.iterdir()) == []


def test_clone_and_pin_read_back_real_git_and_detect_drift(stack: tuple) -> None:
    plan, handler, targets = stack
    assert handler.observe(plan).status == "unavailable"
    handler.apply(plan)
    assert heads(plan) == targets
    assert handler.observe(plan).status == "success"
    before = heads(plan)
    handler.apply(plan)
    assert heads(plan) == before
    repo = plan.workspace / "omnimarket"
    git(repo, "checkout", "--detach", "origin/main")
    assert handler.observe(plan).status == "unavailable"


def test_proven_head_with_user_changes_is_not_ready(stack: tuple) -> None:
    plan, handler, targets = stack
    handler.apply(plan)
    assert heads(plan) == targets
    (plan.workspace / "omnimarket" / "payload").write_text("user edits\n")
    assert handler.observe(plan).status == "action_required"
    assert (plan.workspace / "omnimarket" / "payload").read_text() == "user edits\n"


@pytest.mark.parametrize(
    "mode", ["dirty", "untracked", "local-commit", "hidden-branch"]
)
def test_all_repositories_checked_before_any_checkout(stack: tuple, mode: str) -> None:
    plan, handler, _ = stack
    handler.apply(plan)
    for name in WORKSPACE_REPOSITORIES:
        git(plan.workspace / name, "checkout", "--detach", "origin/main")
    repo = plan.workspace / WORKSPACE_REPOSITORIES[-1]
    if mode in ("dirty", "untracked"):
        (repo / ("payload" if mode == "dirty" else "user-notes")).write_text(
            "keep me\n"
        )
    else:
        previous = git(repo, "rev-parse", "HEAD")
        (repo / "payload").write_text("local-only\n")
        extra = commit(repo, 2023, previous)
        if mode == "local-commit":
            git(repo, "update-ref", "HEAD", extra)
        else:
            git(repo, "read-tree", "--reset", "-u", previous)
            assert git(repo, "status", "--porcelain") == ""
    before = heads(plan)
    assert handler.observe(plan).status == "action_required"
    with pytest.raises(ValueError, match=r"preserv|local|clean|work"):
        handler.apply(plan)
    assert heads(plan) == before


def test_foreign_folder_refused_before_cloning_other_repositories(stack: tuple) -> None:
    plan, handler, _ = stack
    foreign = plan.workspace / WORKSPACE_REPOSITORIES[-1]
    foreign.mkdir()
    (foreign / "user-file").write_text("preserve\n")
    before = sorted(plan.workspace.iterdir())
    with pytest.raises(ValueError, match=r"foreign|repository"):
        handler.apply(plan)
    assert sorted(plan.workspace.iterdir()) == before
    assert (foreign / "user-file").read_text() == "preserve\n"


def test_foreign_remote_is_not_fetched_or_adopted(stack: tuple) -> None:
    plan, handler, _ = stack
    handler.apply(plan)
    repo = plan.workspace / "omnibase_core"
    git(repo, "remote", "set-url", "origin", "https://example.invalid/foreign.git")
    before = heads(plan)
    with pytest.raises(ValueError, match=r"remote|origin"):
        handler.apply(plan)
    assert heads(plan) == before


def test_observe_does_not_create_or_fetch(stack: tuple) -> None:
    plan, handler, _ = stack
    before = sorted(plan.workspace.iterdir())
    assert handler.observe(plan).status == "unavailable"
    assert sorted(plan.workspace.iterdir()) == before


def test_owned_legacy_workspace_adopted_without_changing_user_files(
    tmp_path: Path,
) -> None:
    from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_environment import (
        HandlerSetupEnvironment,
    )

    workspace = tmp_path / "legacy workspace"
    workspace.mkdir()
    marker = workspace / ".omninode-dev-setup"
    marker.write_text("This workspace was created by omninode-dev-setup.\n")
    payload = workspace / "notes"
    payload.write_text("keep these notes\n")
    adapter = HandlerSetupEnvironment(name="workspace", onex_home=tmp_path / "onex")
    plan = ModelSetupPlan(workspace=workspace)
    adapter.apply(plan)
    assert adapter.observe(plan).status == "success"
    assert marker.read_text() == "This workspace was created by omninode-dev-setup.\n"
    assert payload.read_text() == "keep these notes\n"


@pytest.mark.parametrize("indirect", [False, True])
def test_foreign_legacy_marker_does_not_authorize_adoption(
    tmp_path: Path, indirect: bool
) -> None:
    from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_environment import (
        HandlerSetupEnvironment,
    )

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    marker = workspace / ".omninode-dev-setup"
    if indirect:
        outside = tmp_path / "outside"
        outside.write_text("This workspace was created by omninode-dev-setup.\n")
        marker.symlink_to(outside)
    else:
        marker.write_text("foreign marker\n")
    adapter = HandlerSetupEnvironment(name="workspace", onex_home=tmp_path / "onex")
    with pytest.raises(ValueError, match=r"foreign workspace|bounded regular"):
        adapter.apply(ModelSetupPlan(workspace=workspace))
    assert not (workspace / ".onex-setup-workspace").exists()
