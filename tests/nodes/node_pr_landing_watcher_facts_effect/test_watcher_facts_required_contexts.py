# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The watcher facts carry the base branch's required contexts and the passed checks.

The decision reads them to merge on the required contexts only, so a head whose only red is a
check its base branch does not require goes from watcher state to a merge, and a base the
reader did not read goes to a held PR that says why.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_pr_landing_decision_compute.handlers.handler_pr_landing_decision import (
    decide_landing,
)
from omnimarket.nodes.node_pr_landing_watcher_facts_effect.handlers.handler_pr_landing_watcher_facts import (
    map_watcher_state,
)
from omnimarket.nodes.node_pr_landing_watcher_facts_effect.models.model_watcher_facts_overlay import (
    ModelWatcherFactsOverlay,
)

pytestmark = pytest.mark.unit

NODE = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_pr_landing_watcher_facts_effect"
)
STAMP = "2026-11-20T14:00:00Z"
HEAD = "9" * 40
ADVISORY = "delegation-health-check / Delegation Health Check"


def _overlay() -> ModelWatcherFactsOverlay:
    raw = yaml.safe_load(
        (NODE / "watcher_facts.overlay.example.yaml").read_text(encoding="utf-8")
    )
    raw["repositories"] = ["omnimarket"]
    return ModelWatcherFactsOverlay.model_validate(raw)


def _state() -> dict[str, object]:
    runs = [
        ["lint", "completed", "success", STAMP],
        ["CI Summary", "completed", "success", STAMP],
        ["optional-scan", "completed", "skipped", STAMP],
        [ADVISORY, "completed", "failure", STAMP],
        ["typecheck", "in_progress", "", STAMP],
    ]
    return {
        "last_tick": STAMP,
        "prs": {
            "omnimarket#9": {
                "facts": {
                    "repo": "omnimarket",
                    "number": 9,
                    "state": "OPEN",
                    "draft": False,
                    "head_sha": HEAD,
                    "base": "dev",
                    "node_id": "PR_9",
                    "created_at": "2026-11-19T00:00:00Z",
                },
                "ci": {
                    "sha": HEAD,
                    "verdict": "RED",
                    "red": [ADVISORY],
                    "cancelled": [],
                    "red_facts": [[ADVISORY, "failure", "77", STAMP]],
                    "runs": runs,
                },
                "review": {"merge_state": "UNSTABLE"},
                "queued": False,
            }
        },
    }


def test_the_mapper_carries_the_required_contexts_of_the_base_and_the_passed_checks() -> (
    None
):
    facts = map_watcher_state(
        _state(), _overlay(), {"omnimarket:dev": ["CI Summary", "lint"]}
    )
    pr = facts.prs[0]
    assert pr.required_contexts == ("CI Summary", "lint")
    assert pr.passed_checks == ("CI Summary", "lint", "optional-scan")
    assert pr.red_checks == (ADVISORY,)


def test_a_base_the_reader_did_not_read_leaves_the_contexts_unread() -> None:
    assert map_watcher_state(_state(), _overlay()).prs[0].required_contexts is None
    other = map_watcher_state(_state(), _overlay(), {"omnimarket:main": ["lint"]})
    assert other.prs[0].required_contexts is None


def test_watcher_state_to_decision_merges_on_the_required_contexts_only() -> None:
    facts = map_watcher_state(
        _state(), _overlay(), {"omnimarket:dev": ["CI Summary", "lint"]}
    )
    decision = decide_landing(facts)
    assert [(a.kind.value, a.subject) for a in decision.actions] == [
        ("merge", "OmniNode-ai/omnimarket#9")
    ]
    assert decision.non_required_red[0].checks == (ADVISORY,)


def test_watcher_state_to_decision_holds_when_a_required_context_is_still_running() -> (
    None
):
    facts = map_watcher_state(
        _state(), _overlay(), {"omnimarket:dev": ["CI Summary", "lint", "typecheck"]}
    )
    decision = decide_landing(facts)
    assert [a.kind.value for a in decision.actions] == []


def test_watcher_state_to_decision_holds_and_says_why_when_unread() -> None:
    decision = decide_landing(map_watcher_state(_state(), _overlay()))
    assert "merge" not in [a.kind.value for a in decision.actions]
    assert decision.required_contexts_unread[0].pr == "OmniNode-ai/omnimarket#9"
