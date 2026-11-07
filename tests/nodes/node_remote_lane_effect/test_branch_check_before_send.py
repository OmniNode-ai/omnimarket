# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Branch and default branch are checked before a lane is sent (OMN-19998).

The recorded case: a lane was placed naming a branch that had not been pushed yet,
so the repository could not be resolved at placement and the send went ahead
anyway. The node now reads the remote's default branch and the named branch before
send, and a missing one is a typed refusal the caller can act on, never free text.
"""

from __future__ import annotations

import importlib
import os
import subprocess
from importlib.resources import files
from pathlib import Path

import pytest
import yaml
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

from omnimarket.config import service_endpoints
from omnimarket.nodes.node_remote_lane_effect.handlers import HandlerRemoteLaneRef
from omnimarket.nodes.node_remote_lane_effect.models import ModelRemoteLaneRefRequest


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
def remote_endpoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Use the configured URL path with real Git and a local file transport."""
    monkeypatch.setitem(service_endpoints._cfg["github"], "web_url", tmp_path.as_uri())
    owner = tmp_path / "owner_a"
    owner.mkdir()
    return owner / "repo_a.git"


def _handler() -> HandlerRemoteLaneRef:
    contract = yaml.safe_load(
        files("omnimarket.nodes.node_remote_lane_effect")
        .joinpath("contract.yaml")
        .read_text()
    )
    entry = next(
        item
        for item in contract["handler_routing"]["handlers"]
        if item["operation"] == "resolve_remote_lane_ref"
    )
    cls = getattr(
        importlib.import_module(entry["handler"]["module"]), entry["handler"]["name"]
    )
    assert cls is HandlerRemoteLaneRef
    return cls()


@pytest.fixture
def registry(tmp_path: Path, remote_endpoint: Path) -> tuple[Path, str]:
    """A canonical clone of a remote whose default branch is ``dev``; a lane branch
    exists in the clone only, committed but never pushed."""
    source = tmp_path / "source"
    _git("init", "-q", "-b", "dev", str(source))
    _git("-C", str(source), "commit", "-q", "--allow-empty", "-m", "first")
    _git("clone", "-q", "--bare", str(source), str(remote_endpoint))
    clone = tmp_path / "clone"
    _git("clone", "-q", str(remote_endpoint), str(clone))
    _git("-C", str(clone), "checkout", "-q", "-b", "omn-1-lane-branch")
    _git("-C", str(clone), "commit", "-q", "--allow-empty", "-m", "local only")
    return clone, _git("-C", str(source), "rev-parse", "dev")


def test_an_unpushed_branch_is_a_typed_refusal_naming_the_default_branch(
    registry: tuple[Path, str],
) -> None:
    result = _handler().handle(
        ModelRemoteLaneRefRequest(
            repo="repo_a",
            owner="owner_a",
            ref="omn-1-lane-branch",
        )
    )
    assert result.sha is None
    assert result.model_dump(mode="json").get("refusal") == "branch-not-pushed"
    assert result.landing_branch == "dev"
    assert result.error == "the remote has no branch or tag omn-1-lane-branch"


def test_positive_control_the_same_branch_resolves_once_pushed(
    registry: tuple[Path, str],
) -> None:
    clone, _ = registry
    _git("-C", str(clone), "push", "-q", "origin", "omn-1-lane-branch")
    pushed = _git("-C", str(clone), "rev-parse", "omn-1-lane-branch")
    result = _handler().handle(
        ModelRemoteLaneRefRequest(
            repo="repo_a",
            owner="owner_a",
            ref="omn-1-lane-branch",
        )
    )
    assert (result.sha, result.landing_branch, result.model_dump().get("refusal")) == (
        pushed,
        "dev",
        None,
    )


def test_an_empty_remote_is_refused_for_its_default_branch(
    remote_endpoint: Path,
) -> None:
    _git("init", "-q", "--bare", str(remote_endpoint))
    result = _handler().handle(
        ModelRemoteLaneRefRequest(repo="repo_a", owner="owner_a", ref="feature")
    )
    assert result.sha is None
    assert result.model_dump(mode="json").get("refusal") == "no-default-branch"
