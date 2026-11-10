# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared builders for the canary status push tests (OMN-22924): a real bare git remote, the overlay that names it, and a run of the publish node's canary step against it."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

from omnimarket.handlers.emission_sink import InMemoryEmissionSink
from omnimarket.models.floor_17a import (
    ModelCanaryPublishOverlay,
    ModelCanaryStatus,
    ModelFloorSimpleTickRequest,
    ModelFloorSimpleTickResult,
)
from omnimarket.nodes.node_floor_simple_effect.handlers.handler_floor_simple_tick import (
    HandlerFloorSimpleTick,
)
from omnimarket.nodes.node_lab_state_alarm_effect.handlers.channels import (
    AlarmSendRefusedError,
)
from tests.nodes.node_floor_simple_effect.support import OVERLAYS, build_home, environ

NOW = datetime(2026, 11, 7, 15, 30, tzinfo=UTC)
BRANCH = "canary-status"
FILE_NAME = "status.json"


class QuietSlackApi:
    """The alarm channel the canary-only run never posts to."""

    def post(self, text: str) -> str:
        raise AlarmSendRefusedError("the canary-only run posts no alarm")

    def exists(self, message_ts: str) -> bool | None:
        return True


def git(*argv: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *argv],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        env=scrub_git_location_env(os.environ),
    )


def bare_remote(tmp_path: Path) -> Path:
    remote = tmp_path / "floor-canary.git"
    done = git("init", "--bare", "-q", str(remote))
    assert done.returncode == 0, done.stderr
    return remote


def canary_block(remote: str) -> ModelCanaryPublishOverlay:
    return ModelCanaryPublishOverlay(
        remote=remote,
        branch=BRANCH,
        file_name=FILE_NAME,
        floor_max_age_s=900,
        heartbeat_max_age_s=900,
        committer_name="floor-canary",
        committer_email="floor-canary@example.invalid",
    )


def overlay_roots(tmp_path: Path, canary: ModelCanaryPublishOverlay) -> dict[str, str]:
    roots = tmp_path / "overlays"
    shutil.copytree(OVERLAYS, roots)
    path = roots / "node_lab_state_alarm_effect" / "overlay.yaml"
    doc = yaml.safe_load(path.read_text())
    doc["canary"] = canary.model_dump(mode="json")
    doc["channels"][0] = {
        "id": "slack_alerts",
        "kind": "slack",
        "role": "primary",
        "token_ref": "SLACK_BOT_TOKEN",
        "channel_ref": "SLACK_CHANNEL_ID",
    }
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return {"ONEX_SKILL_OVERLAY_ROOTS": str(roots)}


def run_canary_step(
    tmp_path: Path,
    canary: ModelCanaryPublishOverlay,
    *,
    served: tuple[datetime | None, datetime | None] | None,
    publisher: Callable[[ModelCanaryStatus], None] | None = None,
    sink: InMemoryEmissionSink | None = None,
) -> tuple[ModelFloorSimpleTickResult, InMemoryEmissionSink]:
    """The publish node's after-publish step: the floor-simple node run with canary_only, git unmocked unless a publisher is injected."""
    home = build_home(tmp_path)
    env = environ(home) | overlay_roots(tmp_path, canary)
    emissions = sink or InMemoryEmissionSink()
    result = HandlerFloorSimpleTick(
        sink=emissions,
        clock=lambda: NOW,
        served_alarm_ids=lambda: None,
        slack_apis={"slack_alerts": QuietSlackApi()},
        canary_publisher=publisher,
        served_times=lambda: served,
    ).run(ModelFloorSimpleTickRequest(home=str(home), canary_only=True), environ=env)
    return result, emissions


def served_ages(minutes: int = 5) -> tuple[datetime, datetime]:
    moment = NOW - timedelta(minutes=minutes)
    return moment, moment


def read_pushed(remote: Path) -> dict[str, Any] | None:
    """What a reader of the remote's canary-status branch sees; None when the branch or the file is absent."""
    done = git(f"--git-dir={remote}", "show", f"refs/heads/{BRANCH}:{FILE_NAME}")
    if done.returncode != 0:
        return None
    parsed: dict[str, Any] = json.loads(done.stdout)
    return parsed


def read_pushed_text(remote: Path) -> str | None:
    done = git(f"--git-dir={remote}", "show", f"refs/heads/{BRANCH}:{FILE_NAME}")
    return done.stdout if done.returncode == 0 else None


def commit_count(remote: Path) -> int:
    done = git(f"--git-dir={remote}", "rev-list", "--count", f"refs/heads/{BRANCH}")
    return int(done.stdout.strip()) if done.returncode == 0 else 0
