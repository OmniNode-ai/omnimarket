# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Git fixtures and the profile runner shared by the three lane-end node test directories.

A fixture lane directory holds two trees cloned from one bare origin: ``clean`` matches its
upstream and ``ahead`` holds one commit no remote has. The guard tests run real git against
throwaway repositories, the way the OMN-20281 shell guard was exercised.
"""

from __future__ import annotations

import os
import subprocess
from contextlib import nullcontext
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml
from omnibase_core.runtime.runtime_local import RuntimeLocal
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[2]
NODES = ROOT / "src/omnimarket/nodes"
PROFILES = ROOT / "tests/fixtures/lane_closeout/profiles.yaml"
GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
}
PROFILE_PARAMS = ["inmemory", pytest.param("redpanda", marks=pytest.mark.kafka)]


def git(cwd: Path, *argv: str) -> str:
    done = subprocess.run(
        ["git", *argv],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
        env={**scrub_git_location_env(os.environ), **GIT_ENV},
    )
    return done.stdout.strip()


def make_origin(tmp_path: Path) -> Path:
    """A bare origin whose ``main`` holds one commit."""
    bare = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    seed.mkdir()
    git(seed, "init", "-q", "-b", "main")
    (seed / "f.txt").write_text("one\n", encoding="utf-8")
    git(seed, "add", ".")
    git(seed, "commit", "-q", "-m", "one")
    subprocess.run(
        ["git", "clone", "-q", "--bare", str(seed), str(bare)],
        check=True,
        env={**scrub_git_location_env(os.environ), **GIT_ENV},
    )
    return bare


def clone(tmp_path: Path, origin: Path, dest: Path | None = None) -> Path:
    path = dest or tmp_path / "tree"
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "clone", "-q", str(origin), str(path)],
        check=True,
        env={**scrub_git_location_env(os.environ), **GIT_ENV},
    )
    return path


def commit(tree: Path, name: str = "g.txt") -> None:
    (tree / name).write_text("work\n", encoding="utf-8")
    git(tree, "add", name)
    git(tree, "commit", "-q", "-m", f"add {name}")


def wip_branches(origin: Path) -> list[str]:
    out = git(origin, "for-each-ref", "--format=%(refname:short)", "refs/heads/wip/")
    return out.splitlines()


def add_worktree(canon: Path, dest: Path, branch: str) -> Path:
    """A linked worktree of ``canon`` on a new branch that tracks origin/main."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    git(canon, "worktree", "add", "-q", "-b", branch, str(dest), "origin/main")
    return dest


def make_lane_dir(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    """(origin, lane_dir, clean tree, ahead tree): the fixture lane directory with two linked
    worktrees of one canonical clone; ``ahead`` holds one commit no remote has."""
    origin = make_origin(tmp_path)
    canon = clone(tmp_path, origin, tmp_path / "canon")
    lane_dir = tmp_path / "omni_worktrees" / "lane-1"
    clean = add_worktree(canon, lane_dir / "OMN-1" / "alpha", "lane-alpha")
    ahead = add_worktree(canon, lane_dir / "OMN-1" / "beta", "lane-beta")
    commit(ahead)
    return origin, lane_dir, clean, ahead


async def run_over_profile(
    node: str,
    request: BaseModel,
    profile: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Any:
    """Run a node's contract through the local runtime under the in-memory or Redpanda profile."""
    input_path = tmp_path / f"{node}-request.json"
    input_path.write_text(request.model_dump_json())
    overrides = dict(yaml.safe_load(PROFILES.read_text())[profile]["backend_overrides"])
    binding: Any = nullcontext()
    if profile == "redpanda":
        from omnibase_infra.cli.delegate_lane_credentials import resolve_lane_binding
        from omnibase_infra.event_bus.lane_client_transport_binding import (
            bind_lane_client_transport,
        )

        from tests.bus_lane_guard import declared_test_lane, resolve_test_bus_broker

        overrides["kafka_bootstrap"] = resolve_test_bus_broker()
        selection, transport = resolve_lane_binding(declared_test_lane())
        assert selection.bootstrap_servers == overrides["kafka_bootstrap"]
        if transport is not None:
            binding = bind_lane_client_transport(transport)
        else:
            monkeypatch.setenv("KAFKA_SECURITY_PROTOCOL", selection.security_protocol)
            if selection.sasl_mechanism:
                monkeypatch.setenv("KAFKA_SASL_MECHANISM", selection.sasl_mechanism)
    runtime = RuntimeLocal(
        workflow_path=NODES / node / "contract.yaml",
        input_path=input_path,
        state_root=tmp_path / f"state-{uuid4()}",
        backend_overrides=overrides,
        timeout=30,
    )
    with binding:
        await runtime.run_async()
    assert runtime.handler_result is not None, runtime.last_error
    return runtime
