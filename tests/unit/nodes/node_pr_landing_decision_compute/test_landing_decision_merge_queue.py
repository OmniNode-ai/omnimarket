# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19998: a base behind a GitHub merge queue takes a green behind head as it is.

The queue builds every entry on the latest base plus the entries ahead of it, so
an update-branch first costs a push and a fresh CI run (18 to 29 minutes on the
plugin repository this was measured on) and buys nothing. On 2026-10-14, 18 of
19 enqueues there were behind the base when queued and 12 of them merged, so
GitHub accepts a behind PR into such a queue.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.unit.nodes.node_pr_landing_decision_compute.landing_world import (
    run_scenario,
)

RED = {"ci": "red", "red_class": "product", "red_checks": ["unit"]}


def _pr(pr: str, **fields: Any) -> dict[str, Any]:
    return {"pr": pr, **fields}


@pytest.mark.unit
def test_merge_queue_base_queues_every_green_behind_pr_without_an_update() -> None:
    """The measured repository's shape: several green behind PRs on one merge-queue base."""
    behind = {"ci": "green", "merge_state": "behind", "merge_queue": True}
    run_scenario(
        {
            "id": "merge-queue-behind-green",
            "world": {
                "prs": [
                    _pr("acme/plugins#1", **behind),
                    _pr("acme/plugins#2", **behind),
                    _pr("acme/plugins#3", **behind),
                ]
            },
            "ticks": [
                {
                    "expect": {
                        "actions": [
                            "merge acme/plugins#1 h1",
                            "merge acme/plugins#2 h1",
                            "merge acme/plugins#3 h1",
                        ]
                    }
                }
            ],
        }
    )


@pytest.mark.unit
def test_merge_queue_base_leaves_a_pending_behind_head_to_finish() -> None:
    """An update would cancel the run in flight and start it again; the queue rebases anyway."""
    run_scenario(
        {
            "id": "merge-queue-behind-pending",
            "world": {
                "prs": [
                    _pr(
                        "acme/plugins#1",
                        ci="pending",
                        merge_state="behind",
                        merge_queue=True,
                    )
                ]
            },
            "ticks": [
                {"expect": {"actions": []}},
                {
                    "events": [
                        {"ci": {"pr": "acme/plugins#1", "result": "green"}},
                    ],
                    "expect": {"actions": ["merge acme/plugins#1 h1"]},
                },
            ],
        }
    )


@pytest.mark.unit
def test_merge_queue_base_still_updates_a_red_behind_head() -> None:
    """A red head may be red for its base: the update stays the first move there."""
    run_scenario(
        {
            "id": "merge-queue-behind-red",
            "world": {
                "prs": [
                    _pr(
                        "acme/plugins#1",
                        **RED,
                        merge_state="behind",
                        merge_queue=True,
                    )
                ]
            },
            "ticks": [{"expect": {"actions": ["update_branch acme/plugins#1"]}}],
        }
    )


@pytest.mark.unit
def test_merge_queue_base_leaves_an_armed_owned_head_in_the_queue() -> None:
    """The armed-green refresh is a strict-base move; a queued head is never pushed to."""
    run_scenario(
        {
            "id": "merge-queue-armed-owned",
            "world": {
                "prs": [
                    _pr(
                        "acme/plugins#1",
                        ci="green",
                        merge_state="behind",
                        merge_queue=True,
                        suspensions=["owned"],
                        auto_merge_head="h1",
                    )
                ]
            },
            "ticks": [{"expect": {"actions": []}}],
        }
    )


@pytest.mark.unit
def test_strict_base_without_a_queue_still_updates_first() -> None:
    """Control: the same green behind head on a plain strict base is updated first."""
    run_scenario(
        {
            "id": "strict-behind-green",
            "world": {"prs": [_pr("acme/app#1", ci="green", merge_state="behind")]},
            "ticks": [{"expect": {"actions": ["update_branch acme/app#1"]}}],
        }
    )
