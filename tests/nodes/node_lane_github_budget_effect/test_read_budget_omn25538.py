# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A lane reads failure text inside a declared budget, and a cache-only read costs none (OMN-25538).

Two lanes on a lab host stopped at `GitHub read budget=8 exhausted` while reading the failing
jobs of three pull requests: every distinct job is one gateway cache miss, and the overlay
allowed eight per lane. The node ships its overlay example with the value the deployment's
overlay declares, so the tests read that file, never a number typed into the test.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

import omnimarket.nodes.node_lane_github_budget_effect as node
from omnimarket.nodes.node_lane_github_budget_effect.handlers.handler_lane_github_budget import (
    HandlerLaneGithubBudget,
    gateway_cache_misses_per_lane,
)
from omnimarket.nodes.node_lane_github_budget_effect.models.model_lane_github_budget import (
    ModelLaneGithubCall,
)

pytestmark = pytest.mark.unit

EXAMPLE = Path(node.__file__).parent / "overlay.example.yaml"

# `job-log` is the one GitHub read and holds the whole log in the gateway; `job-log-cached`
# answers a held job and otherwise exits non-zero without a call, as the real verb does.
FAKE_ONEX = """#!/bin/sh
verb="$2"
job=""
prev=""
for arg in "$@"; do
  if [ "$prev" = "--job-id" ]; then job="$arg"; fi
  prev="$arg"
done
case "$verb" in
  job-log-cached)
    if [ -f "$TEST_HELD/$job" ]; then printf 'cached %s\\n' "$job"; exit 0; fi
    exit 1;;
  job-log)
    : > "$TEST_HELD/$job"
    printf 'tail of %s\\n' "$job"; exit 0;;
esac
exit 64
"""


@pytest.fixture(autouse=True)
def shipped_overlay(monkeypatch):
    monkeypatch.setenv("ONEX_LANE_GITHUB_BUDGET_OVERLAY", str(EXAMPLE))


def lane(tmp_path, budget):
    tool = tmp_path / "onex"
    tool.write_text(FAKE_ONEX)
    tool.chmod(0o755)
    (tmp_path / "held").mkdir()
    policy = {
        "lane": "lane-budget",
        "budget": budget,
        "onex": str(tool),
        "log": str(tmp_path / "calls.log"),
        "spent": str(tmp_path / "spent"),
        "cache": str(tmp_path / "cache"),
    }
    env = {
        "PATH": os.environ["PATH"],
        "TEST_HELD": str(tmp_path / "held"),
    }
    return policy, env


def read(policy, env, verb, job):
    return HandlerLaneGithubBudget().handle(
        ModelLaneGithubCall(
            policy, "onex", ("scm", verb, "o/r", "--job-id", str(job)), env
        )
    )


def spent(policy):
    path = Path(policy["spent"])
    return int(path.read_text()) if path.exists() else 0


def test_the_shipped_overlay_lets_a_lane_read_the_failing_jobs_of_three_pull_requests():
    # Eight distinct jobs ended two lanes; three pull requests with a few failing jobs each
    # need more than that. The value is what the deployment's overlay declares.
    declared = yaml.safe_load(EXAMPLE.read_text())["gateway_cache_misses_per_lane"]
    assert declared == gateway_cache_misses_per_lane()
    assert declared == 24


def test_reads_under_the_declared_budget_are_served_and_the_next_one_is_refused(
    tmp_path,
):
    allowance = gateway_cache_misses_per_lane()
    policy, env = lane(tmp_path, allowance)
    served = [read(policy, env, "job-log", job) for job in range(1, allowance + 1)]
    assert [r.code for r in served] == [0] * allowance
    assert served[-1].stdout == f"tail of {allowance}\n"
    assert spent(policy) == allowance
    refused = read(policy, env, "job-log", allowance + 1)
    assert refused.code == 78
    assert f"GitHub read budget={allowance} exhausted" in refused.stderr
    assert spent(policy) == allowance


def test_a_budget_above_the_declared_allowance_is_invalid(tmp_path):
    policy, env = lane(tmp_path, gateway_cache_misses_per_lane() + 1)
    with pytest.raises(ValueError, match="invalid lane read budget"):
        read(policy, env, "job-log", 1)


def test_a_cache_only_read_spends_no_unit_and_the_same_read_without_it_spends_one(
    tmp_path,
):
    policy, env = lane(tmp_path, 2)
    (tmp_path / "held" / "7").write_text("")
    before = spent(policy)
    cached = read(policy, env, "job-log-cached", 7)
    assert cached.code == 0
    assert spent(policy) == before
    rows = [
        line.split("\t")
        for line in Path(policy["log"]).read_text().splitlines()
        if line
    ]
    assert [(row[3], row[7]) for row in rows] == [("scm job-log-cached", "cache")]
    read(policy, env, "job-log", 8)
    assert spent(policy) == before + 1
