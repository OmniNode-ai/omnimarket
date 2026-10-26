# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Execute the five local G3 nodes through their packaged typed contracts."""

from __future__ import annotations

import importlib
import os
import subprocess
from pathlib import Path
from typing import Any, get_type_hints

import pytest
import yaml
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)
from pydantic import BaseModel, ValidationError

pytestmark = pytest.mark.unit


def descendant_repo(path: Path) -> tuple[str, str]:
    identity = {
        "GIT_AUTHOR_NAME": "fixture",
        "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
        "GIT_COMMITTER_NAME": "fixture",
        "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
    }

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(path), "-c", "commit.gpgsign=false", *args],
            capture_output=True,
            text=True,
            check=True,
            env={**scrub_git_location_env(os.environ), **identity},
        ).stdout.strip()

    git("init", "-q", "-b", "main")
    (path / "file.txt").write_text("base\n")
    git("add", "file.txt")
    git("commit", "-q", "-m", "base fixture")
    live = git("rev-parse", "HEAD")
    (path / "file.txt").write_text("base\nnew\n")
    git("add", "file.txt")
    git("commit", "-q", "-m", "descendant fixture")
    return live, git("rev-parse", "HEAD")


@pytest.mark.parametrize(
    "node",
    [
        "node_landing_placement_wait_effect",
        "node_landing_not_started_effect",
        "node_install_superset_guard_effect",
        "node_landing_tick_speed_effect",
    ],
)
def test_g3_local_invocation(node: str, tmp_path: Path) -> None:
    package = importlib.import_module(f"omnimarket.nodes.{node}")
    assert package.__file__ is not None
    directory = Path(package.__file__).parent
    contract = yaml.safe_load((directory / "contract.yaml").read_text())
    metadata = yaml.safe_load((directory / "metadata.yaml").read_text())
    assert contract["metadata"]["transport_type"] == "local"
    assert contract["runtime_profiles"] == []
    assert contract["descriptor"]["runtime_profiles"] == []
    assert contract["event_bus"] == {"subscribe_topics": [], "publish_topics": []}
    assert contract["runtime_dispatch"]["external_trigger"].strip()
    assert metadata["capabilities"]["full_runtime"] is False
    binding = contract["handler"]
    handler = getattr(importlib.import_module(binding["module"]), binding["class"])
    assert getattr(package, binding["class"]) is handler

    def model(ref: str) -> type[BaseModel]:
        module, _, name = ref.rpartition(".")
        candidate = getattr(importlib.import_module(module), name)
        assert isinstance(candidate, type)
        assert issubclass(candidate, BaseModel)
        return candidate

    request_model = model(binding["input_model"])
    result_model = model(binding["output_model"])
    assert (
        model(f"{contract['input_model']['module']}.{contract['input_model']['name']}")
        is request_model
    )
    assert (
        model(
            f"{contract['output_model']['module']}.{contract['output_model']['name']}"
        )
        is result_model
    )
    hints = get_type_hints(handler.handle)
    assert hints["request"] is request_model
    assert hints["return"] is result_model
    payload: dict[str, Any]
    if node == "node_landing_placement_wait_effect":
        payload = {
            "worker_dir": tmp_path,
            "lease": {
                "deadline_at": "2030-01-01T01:00:00Z",
                "dispatched_at": "2030-01-01T00:00:00Z",
                "placed_at": "2030-01-01T00:50:00Z",
            },
            "engine_rc": 143,
            "now": "2030-01-01T01:00:00Z",
        }
        expected = {"cause": "placement-wait"}
    elif node == "node_landing_not_started_effect":
        payload = {
            "state_dir": tmp_path,
            "outcomes": (),
            "refused": (),
            "now": "2030-01-01T01:00:00Z",
            "tick": 1,
        }
        expected = {"rows": ()}
    elif node == "node_install_superset_guard_effect":
        live, new = descendant_repo(tmp_path)
        payload = {
            "repo": tmp_path,
            "live_commit": live,
            "new_commit": new,
            "lane": "lane-a",
            "main_ref": "main",
        }
        expected = {"verdict": "contained", "live_commit": live, "new_commit": new}
    else:
        payload = {
            "operation": "ledger_indexed",
            "state_dir": tmp_path,
            "rows_cache": {},
            "read": lambda: ["row"],
        }
        expected = {"value": ["row"]}
    result = handler().handle(request_model.model_validate(payload))
    assert isinstance(result, result_model)
    assert {key: getattr(result, key) for key in expected} == expected
    with pytest.raises(ValidationError):
        request_model.model_validate({})
    with pytest.raises(ValidationError):
        request_model.model_validate({**payload, "undeclared": True})
    with pytest.raises(ValidationError):
        request_model.model_validate(payload).state_dir = tmp_path
