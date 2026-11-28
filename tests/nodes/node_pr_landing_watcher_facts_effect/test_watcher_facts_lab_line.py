# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The watcher facts mapper reads the newest Lab line into the lab_line fact (OMN-26736)."""

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
OLD = "1" * 40
KEY = "OmniNode-ai/omnimarket#9"


def _overlay() -> ModelWatcherFactsOverlay:
    raw = yaml.safe_load(
        (NODE / "watcher_facts.overlay.example.yaml").read_text(encoding="utf-8")
    )
    raw["repositories"] = ["omnimarket"]
    return ModelWatcherFactsOverlay.model_validate(raw)


def _state(body: str) -> dict[str, object]:
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
                    "body": body,
                },
                "ci": {
                    "sha": HEAD,
                    "verdict": "GREEN",
                    "runs": [["lint", "completed", "success", STAMP]],
                },
                "review": {"merge_state": "CLEAN"},
                "queued": False,
            }
        },
    }


REQ = {"omnimarket:dev": ["lint"]}


def _kinds(state: dict[str, object], lab: dict[str, list[str]] | None) -> list[str]:
    facts = map_watcher_state(state, _overlay(), REQ, lab)
    return [a.kind.value for a in decide_landing(facts).actions]


def test_body_lab_line_naming_an_older_head_with_comments_read_is_not_merged() -> None:
    state = _state(f"Lab: host h1, head={OLD}, ran tests, passed")
    facts = map_watcher_state(state, _overlay(), REQ, {KEY: []})
    assert facts.prs[0].lab_line is not None
    assert facts.prs[0].lab_line.named_head == OLD
    assert "merge" not in _kinds(state, {KEY: []})


def test_a_newer_comment_lab_line_naming_the_head_wins_over_the_body() -> None:
    state = _state(f"Lab: host h1, head={OLD}, passed")
    comments = {KEY: [f"Lab: head={HEAD} host h1 passed"]}
    facts = map_watcher_state(state, _overlay(), REQ, comments)
    assert facts.prs[0].lab_line is not None
    assert facts.prs[0].lab_line.named_head == HEAD
    assert "merge" in _kinds(state, comments)


def test_body_lab_line_naming_the_head_merges_with_comments_unread() -> None:
    state = _state(f"Lab: host h1, head={HEAD}, passed")
    assert "merge" in _kinds(state, None)


def test_older_head_with_comments_unread_leaves_the_fact_unread_not_asserted() -> None:
    state = _state(f"Lab: host h1, head={OLD}, passed")
    facts = map_watcher_state(state, _overlay(), REQ)
    assert facts.prs[0].lab_line is None


def test_no_lab_line_anywhere_with_comments_read_is_needs_proof() -> None:
    state = _state("just a body")
    facts = map_watcher_state(state, _overlay(), REQ, {KEY: ["unrelated"]})
    assert facts.prs[0].lab_line is not None
    assert facts.prs[0].lab_line.named_head is None
