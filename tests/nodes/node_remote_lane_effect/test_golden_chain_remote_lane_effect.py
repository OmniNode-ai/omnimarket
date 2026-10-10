# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain: the effect contract resolves and reads refs through real git (OMN-20669).

The repositories are built in a temporary directory: a source repository whose
default branch is not ``main``, a bare remote and a canonical clone of it under a
registry root. No network is used.
"""

from __future__ import annotations

import importlib
import os
import subprocess
from importlib.resources import files
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

from omnimarket.nodes.node_remote_lane_effect.handlers import HandlerRemoteLaneRef
from omnimarket.nodes.node_remote_lane_effect.models import ModelRemoteLaneRefRequest

NAME = "node_remote_lane_effect"


def _git(*args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        capture_output=True,
        text=True,
        check=True,
        env=scrub_git_location_env(os.environ),
    )
    return done.stdout.strip()


@pytest.fixture
def registry(tmp_path: Path) -> tuple[Path, str, str]:
    """A registry root holding ``repo_a``'s canonical clone; its remote's HEAD is ``dev``."""
    source = tmp_path / "source"
    _git("init", "-q", "-b", "dev", str(source))
    _git("-C", str(source), "commit", "-q", "--allow-empty", "-m", "first")
    _git("-C", str(source), "branch", "feature")
    _git("-C", str(source), "commit", "-q", "--allow-empty", "-m", "second")
    _git("-C", str(source), "tag", "v1")
    _git("clone", "-q", "--bare", str(source), str(tmp_path / "remote.git"))
    home = tmp_path / "registry"
    home.mkdir()
    _git("clone", "-q", str(tmp_path / "remote.git"), str(home / "repo_a"))
    dev = _git("-C", str(source), "rev-parse", "dev")
    feature = _git("-C", str(source), "rev-parse", "feature")
    return home, dev, feature


def _contract() -> dict[str, Any]:
    return yaml.safe_load(
        files(f"omnimarket.nodes.{NAME}").joinpath("contract.yaml").read_text()
    )


def test_contract_declares_dispatch_and_one_bound_operation() -> None:
    contract = _contract()
    assert contract["name"] == NAME
    assert "event_bus" not in contract
    dispatch = contract["runtime_dispatch"]
    assert (
        dispatch["command_topic"]
        == "onex.cmd.omnimarket.remote-lane-effect-requested.v1"
    )
    assert set(dispatch["terminal_events"]) == {"success", "failure"}
    (entry,) = contract["handler_routing"]["handlers"]
    assert entry["operation"] == "resolve_remote_lane_ref"
    handler = getattr(
        importlib.import_module(entry["handler"]["module"]), entry["handler"]["name"]
    )
    assert handler is HandlerRemoteLaneRef
    module, _, name = entry["input_model"].rpartition(".")
    assert getattr(importlib.import_module(module), name) is ModelRemoteLaneRefRequest


def test_no_ref_builds_from_the_landing_branch_read_through_the_clone(
    registry: tuple[Path, str, str],
) -> None:
    home, dev, _ = registry
    result = HandlerRemoteLaneRef().handle(
        ModelRemoteLaneRefRequest(repo="repo_a", owner="owner_a", omni_home=str(home))
    )
    assert (result.sha, result.landing_branch, result.error) == (dev, "dev", None)
    assert result.remote == "origin"


def test_a_named_branch_or_tag_resolves_and_a_named_sha_passes(
    registry: tuple[Path, str, str],
) -> None:
    home, dev, feature = registry
    handler = HandlerRemoteLaneRef()
    branch = handler.handle(
        ModelRemoteLaneRefRequest(
            repo="repo_a", owner="owner_a", ref="feature", omni_home=str(home)
        )
    )
    assert (branch.sha, branch.landing_branch) == (feature, None)
    tag = handler.handle(
        ModelRemoteLaneRefRequest(
            repo="repo_a", owner="owner_a", ref="v1", omni_home=str(home)
        )
    )
    assert tag.sha == dev
    named = handler.handle(
        ModelRemoteLaneRefRequest(repo="repo_a", owner="owner_a", sha=feature)
    )
    assert named.sha == feature


def test_an_inherited_git_location_does_not_redirect_the_read(
    registry: tuple[Path, str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hook's GIT_DIR names another repository; the read still goes through the clone."""
    home, dev, _ = registry
    monkeypatch.setenv("GIT_DIR", str(home.parent / "source" / ".git"))
    result = HandlerRemoteLaneRef().handle(
        ModelRemoteLaneRefRequest(repo="repo_a", owner="owner_a", omni_home=str(home))
    )
    assert (result.sha, result.landing_branch) == (dev, "dev")


def test_the_option_after_the_remote_reads_no_landing_branch(
    registry: tuple[Path, str, str],
) -> None:
    """Positive control: the old argv order prints no ``ref:`` line; the node's order does."""
    home, _, _ = registry
    clone = str(home / "repo_a")
    old = _git("-C", clone, "ls-remote", "origin", "--symref", "HEAD")
    new = _git("-C", clone, "ls-remote", "--symref", "origin", "HEAD")
    assert old
    assert "ref: refs/heads/dev" not in old
    assert "ref: refs/heads/dev\tHEAD" in new
