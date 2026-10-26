# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A queue retry retains authored inputs and releases the previous host pin."""

import pytest

from omnimarket.nodes.node_lane_queue_argv_compute import NodeLaneQueueArgvCompute
from omnimarket.nodes.node_lane_queue_argv_compute.models.model_lane_queue_argv import (
    ModelLaneQueueArgvRequest,
)

pytestmark = pytest.mark.unit


def render(**over):
    given = {"lane": "lane-a", "model": "codex", "host": "host-a", **over}
    return (
        NodeLaneQueueArgvCompute()
        .handle(ModelLaneQueueArgvRequest(given, "pinned.md"))
        .argv
    )


def test_retry_reuses_the_pinned_brief_without_the_host():
    assert render() == (
        "run",
        "--detach",
        "--no-placement-wait",
        "--brief",
        "pinned.md",
        "--lane",
        "lane-a",
        "--model",
        "codex",
    )


def test_sha_wins_over_ref_and_lists_keep_order():
    argv = render(
        repo="repo-a",
        sha="a" * 40,
        ref="dev",
        pr=["repo-a#1", "repo-a#2"],
        context=["one", "two"],
        require_kb=True,
        no_plugins=True,
    )
    assert "--ref" not in argv
    assert argv[argv.index("--sha") + 1] == "a" * 40
    assert argv[argv.index("--pr") :] == (
        "--pr",
        "repo-a#1",
        "--pr",
        "repo-a#2",
        "--context",
        "one",
        "--context",
        "two",
        "--require-kb",
        "--no-plugins",
    )


@pytest.mark.parametrize("value", [None, "", []])
def test_unset_inputs_are_omitted(value):
    argv = render(ticket=value, repo=value, sha="a" * 40)
    assert "--ticket" not in argv
    assert "--repo" not in argv
    assert "--sha" not in argv
