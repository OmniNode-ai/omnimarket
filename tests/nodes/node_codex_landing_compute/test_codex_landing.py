# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Landing shell preparation and failure classifications are deterministic."""

import json
import os
import subprocess
import sys

import pytest
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

from omnimarket.nodes.node_codex_landing_compute import NodeCodexLandingCompute
from omnimarket.nodes.node_codex_landing_compute.handlers.handler_codex_landing import (
    landing_failure,
)
from omnimarket.nodes.node_codex_landing_compute.models.model_codex_landing import (
    ModelCodexLandingRequest,
)

pytestmark = pytest.mark.unit


def test_exact_head_json_reader_accepts_the_gateway_response():
    result = NodeCodexLandingCompute().handle(ModelCodexLandingRequest("/worktree"))
    facts = {
        "operation": "pr_text",
        "head_ref": "topic/fix",
        "head_sha": "a" * 40,
        "state": "OPEN",
        "is_cross_repository": False,
    }
    proc = subprocess.run(
        [sys.executable, "-c", result.json_reader],
        input=json.dumps({"result": facts}),
        capture_output=True,
        text=True,
        check=True,
    )
    assert proc.stdout.strip() == "topic/fix\t" + "a" * 40 + "\tfalse\tOPEN"


@pytest.mark.parametrize(
    "facts", [{}, {"operation": "pr_text", "head_ref": "bad\nref"}]
)
def test_json_reader_refuses_incomplete_or_unsafe_facts(facts):
    result = NodeCodexLandingCompute().handle(ModelCodexLandingRequest("/worktree"))
    proc = subprocess.run(
        [sys.executable, "-c", result.json_reader],
        input=json.dumps({"result": facts}),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode != 0


@pytest.mark.parametrize(
    ("steps", "shape"),
    [
        ([("COMMIT", "failed attempt=1")], "commit-hook-failed"),
        (
            [("COMMIT", "failed attempt=1"), ("COMMIT", "unchanged hook=spdx-header")],
            "commit-hook-unchanged",
        ),
        (
            [("COMMIT", "ok sha=abc"), ("PUSH", "failed non-fast-forward")],
            "push-non-fast-forward",
        ),
        (
            [("COMMIT", "ok sha=abc"), ("PUSH", "failed stale info")],
            "push-lease-refused",
        ),
        (
            [
                ("COMMIT", "ok sha=abc"),
                ("PUSH", "failed hook=prepush-deploy-scope-dod"),
            ],
            "push-hook-refused",
        ),
        (
            [("COMMIT", "ok sha=abc"), ("PUSH", "skipped detached HEAD")],
            "push-detached-head",
        ),
        (
            [("COMMIT", "ok sha=abc"), ("PUSH", "failed permission denied")],
            "push-refused-access",
        ),
    ],
)
def test_landing_failure_status_names_each_shape(steps, shape):
    failure = landing_failure(steps)
    assert failure is not None
    assert failure["shape"] == shape
    assert failure["kind"] == "landing-failed"


@pytest.mark.parametrize(
    "steps", [[], [("COMMIT", "none")], [("COMMIT", "ok"), ("PUSH", "ok branch=fix")]]
)
def test_landing_failure_status_is_none_when_nothing_was_lost(steps):
    assert landing_failure(steps) is None


def test_generated_cleanup_removes_only_untracked_bytecode(tmp_path):
    env = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull)
    subprocess.run(
        ["git", "init", "-q", str(tmp_path)],
        env=scrub_git_location_env(env),
        check=True,
    )
    (tmp_path / "kept.pyc").write_bytes(b"tracked")
    subprocess.run(
        ["git", "-C", str(tmp_path), "add", "kept.pyc"],
        env=scrub_git_location_env(env),
        check=True,
    )
    (tmp_path / "gone.pyc").write_bytes(b"bytecode")
    (tmp_path / "source.py").write_text("pass\n")
    result = NodeCodexLandingCompute().handle(ModelCodexLandingRequest(str(tmp_path)))
    subprocess.run(
        ["bash", "-c", result.clean_command],
        env=scrub_git_location_env(env),
        check=True,
    )
    assert not (tmp_path / "gone.pyc").exists()
    assert (tmp_path / "kept.pyc").read_bytes() == b"tracked"
    assert (tmp_path / "source.py").read_text() == "pass\n"
