# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared helpers of the goal-scope admission tests (OMN-23851): the recorded cases and the request they build."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from omnimarket.models.goal_scope_admission import ModelGoalScopeAdmissionRequest

FIXTURE = Path(__file__).parent / "fixtures" / "recorded_cases.json"
CASES: list[dict[str, Any]] = json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]
READ_BOUNDS = {
    "endpoint": "https://linear.invalid/graphql",
    "cache_seconds": 3600,
    "reads_per_tick": 40,
    "read_timeout_seconds": 5,
}


def request_of(case: dict[str, Any]) -> ModelGoalScopeAdmissionRequest:
    """The admission request of one recorded case: the rule, the scope, the ancestry facts and the tickets a read failed on."""
    scope = case["scope"]
    return ModelGoalScopeAdmissionRequest.model_validate(
        {
            "candidates": case["candidates"],
            "rule": {
                "roots": case["roots"],
                "max_depth": case["max_depth"],
                **READ_BOUNDS,
            },
            "scope": {**scope, **READ_BOUNDS} if scope else None,
            "ticket_facts": case["facts"],
            "unread_tickets": case["unread_tickets"],
        }
    )
