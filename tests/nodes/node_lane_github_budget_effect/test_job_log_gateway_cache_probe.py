# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A window of a job log the gateway already holds is not a GitHub read (OMN-22862).

Measured on a lab host: 110 of 462 successful job-log reads were another window of a job
already read, and each was charged one of the lane's eight units although the gateway
serves it from its own cache. The lane wrapper now asks the gateway cache first.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from omnimarket.nodes.node_lane_github_budget_effect.handlers.handler_lane_github_budget import (
    HandlerLaneGithubBudget,
    quota_consumers,
)
from omnimarket.nodes.node_lane_github_budget_effect.models.model_lane_github_budget import (
    ModelLaneGithubCall,
)

pytestmark = pytest.mark.unit

# A stand-in for the onex CLI. `job-log` is the one GitHub read: it records a network
# call and leaves the whole log held in the gateway. `job-log-cached` answers only for
# a held job and otherwise exits non-zero without a call, as the real verb does.
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
    if [ -f "$TEST_HELD/$job" ]; then printf 'window of %s\\n' "$job"; exit 0; fi
    exit 1;;
  job-log)
    printf 'call %s\\n' "$job" >> "$TEST_NETWORK"
    : > "$TEST_HELD/$job"
    printf 'tail of %s\\n' "$job"; exit 0;;
esac
exit 64
"""


@pytest.fixture
def lane(tmp_path):
    tool = tmp_path / "onex"
    tool.write_text(FAKE_ONEX)
    tool.chmod(0o755)
    (tmp_path / "held").mkdir()
    policy = {
        "lane": "lane-probe",
        "budget": 2,
        "onex": str(tool),
        "log": str(tmp_path / "calls.log"),
        "spent": str(tmp_path / "spent"),
        "cache": str(tmp_path / "cache"),
    }
    env = {
        "PATH": os.environ["PATH"],
        "TEST_NETWORK": str(tmp_path / "network"),
        "TEST_HELD": str(tmp_path / "held"),
    }
    return policy, env, tmp_path


def read(lane, job, *window):
    policy, env, _ = lane
    return HandlerLaneGithubBudget().handle(
        ModelLaneGithubCall(
            policy, "onex", ("scm", "job-log", "o/r", "--job-id", job, *window), env
        )
    )


def spent(lane):
    path = Path(lane[0]["spent"])
    return int(path.read_text()) if path.exists() else 0


def network(lane):
    path = lane[2] / "network"
    return path.read_text().splitlines() if path.exists() else []


def actions(lane):
    rows = Path(lane[0]["log"]).read_text().splitlines()
    return [row.split("\t")[-1] for row in rows]


def test_more_windows_of_a_held_job_spend_no_unit_and_make_no_github_call(lane):
    first = read(lane, "7")
    assert (first.code, first.stdout) == (0, "tail of 7\n")
    assert spent(lane) == 1
    for offset in ("65536", "131072", "196608"):
        window = read(lane, "7", "--offset-bytes", offset, "--length-bytes", "65536")
        assert (window.code, window.stdout) == (0, "window of 7\n")
    assert spent(lane) == 1
    assert network(lane) == ["call 7"]
    assert actions(lane) == ["allowed", "cache", "cache", "cache"]


def test_a_job_the_gateway_does_not_hold_is_still_charged_and_the_bound_holds(lane):
    assert read(lane, "1").code == 0
    assert read(lane, "2").code == 0
    assert spent(lane) == 2
    refused = read(lane, "3")
    assert refused.code == 78
    assert "budget=2" in refused.stderr
    assert network(lane) == ["call 1", "call 2"]
    # The shared cache stays readable after exhaustion: a held job still answers.
    assert read(lane, "1", "--offset-bytes", "65536").stdout == "window of 1\n"
    assert spent(lane) == 2


def test_a_cli_without_the_cache_verb_falls_back_to_the_charged_read(lane):
    # An older CLI refuses the unknown verb with a usage error: the read is charged.
    Path(lane[0]["onex"]).write_text(
        '#!/bin/sh\n[ "$2" = job-log-cached ] && exit 2\n'
        'printf "call\\n" >> "$TEST_NETWORK"\nprintf "tail\\n"\n'
    )
    assert read(lane, "5").stdout == "tail\n"
    assert read(lane, "5", "--offset-bytes", "65536").stdout == "tail\n"
    assert spent(lane) == 2
    assert len(network(lane)) == 2


def test_a_hung_cache_probe_is_a_miss_not_a_hang(lane, monkeypatch):
    from omnimarket.nodes.node_lane_github_budget_effect.handlers import (
        handler_lane_github_budget as module,
    )

    monkeypatch.setitem(module.POLICY, "job_log_cache_probe_timeout_seconds", 1)
    Path(lane[0]["onex"]).write_text(
        '#!/bin/sh\n[ "$2" = job-log-cached ] && exec sleep 30\n'
        'printf "call\\n" >> "$TEST_NETWORK"\nprintf "tail\\n"\n'
    )
    assert read(lane, "6").stdout == "tail\n"
    assert spent(lane) == 1


def test_injected_break_without_the_probe_each_window_costs_a_unit(lane, monkeypatch):
    # The named break: the pre-fix behaviour. Every other window of one held job is a
    # charged read, and the lane is out of reads after two windows of a single log.
    monkeypatch.setattr(
        HandlerLaneGithubBudget, "probe_gateway_job_log", lambda *_unused: None
    )
    read(lane, "7")
    read(lane, "7", "--offset-bytes", "65536")
    assert spent(lane) == 2
    assert read(lane, "7", "--offset-bytes", "131072").code == 78


def test_quota_consumers_count_one_charged_read_for_a_job_read_in_four_windows(lane):
    read(lane, "7")
    for offset in ("65536", "131072", "196608"):
        read(lane, "7", "--offset-bytes", offset, "--length-bytes", "65536")
    assert actions(lane) == ["allowed", "cache", "cache", "cache"]
    result = quota_consumers(Path(lane[0]["log"]), None, datetime.now(UTC))
    assert result["logged"] == 1
    assert result["top"] == [["lane-probe", 1]]
