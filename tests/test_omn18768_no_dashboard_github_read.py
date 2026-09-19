# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18768 AC5 — the serving path never reads the GitHub API.

Reading the GitHub org runners API from the dashboard bridge was WEIGHED AND
REJECTED for this panel. It would have been far less work: one REST call
answers "what runners are running" directly, and it is the call the runner
monitor already makes. It is rejected because it is an ad-hoc read over a
third-party API PRESENTED AS a projection, and the doctrine the whole
observability epic rests on is that the dashboard renders projections.

The consequence, stated so nobody has to rediscover it: the runner state the
dashboard shows is as fresh as the monitor's 3-minute cycle and no fresher,
and when the bus is down the panel goes stale rather than silently falling
back to a live read that only that one panel has. Both are the intended
trade — a panel whose freshness is a property of the pipeline, not of which
code path happened to answer.

This guard is scoped to what THIS repo can prove: the whole runner-fleet
projection path here — the reducer, the writer, the models, the contract —
contains no HTTP read of GitHub at all. The omnidash-side half of the AC is a
grep over that repo, quoted in the PR body; it is not asserted here because a
test in this repo that passed while omnidash grew a GitHub read would be worse
than no test.
"""

from __future__ import annotations

from pathlib import Path

import pytest

NODE_DIR = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_runner_fleet"
)

# Substrings that would indicate a direct third-party read on this path. Kept
# narrow and literal: a broad "github" match would fire on every docstring that
# explains WHY the read is absent, which is how a guard gets deleted.
FORBIDDEN = (
    "api.github.com",
    "actions/runners",
    "gh api",
)

pytestmark = pytest.mark.unit


def _source_files() -> list[Path]:
    return sorted(
        path
        for pattern in ("**/*.py", "**/*.yaml", "**/*.sql")
        for path in NODE_DIR.glob(pattern)
        if "__pycache__" not in path.parts
    )


def test_no_dashboard_or_bridge_github_api_read_is_added() -> None:
    """AC5 — nothing on this projection path reads the GitHub API."""
    offenders: list[str] = []
    for path in _source_files():
        text = path.read_text(encoding="utf-8")
        for needle in FORBIDDEN:
            if needle in text:
                offenders.append(f"{path.relative_to(NODE_DIR)}: {needle}")
    assert not offenders, (
        "the runner-fleet projection path must be fed from the bus, never from "
        "a direct GitHub read: " + "; ".join(offenders)
    )


def test_the_node_declares_its_only_input_as_the_bus_topic() -> None:
    """Absence of a forbidden string is weak evidence on its own. The positive
    statement is that the node's ONLY declared input is the bus topic."""
    import yaml

    with open(NODE_DIR / "contract.yaml") as handle:
        contract = yaml.safe_load(handle)
    assert contract["event_bus"]["subscribe_topics"] == [
        "onex.evt.omnibase-infra.runner-fleet.v1"
    ]
    # And it declares exactly one table, its own read model — no second source.
    assert len(contract["db_io"]["db_tables"]) == 1


def test_the_guard_can_actually_fail() -> None:
    """A positive control. An empty result is not evidence of absence, and a
    guard whose matcher never matched anything would report clean forever."""
    sample = "resp = requests.get('https://api.github.com/orgs/x/actions/runners')"
    assert any(needle in sample for needle in FORBIDDEN)
