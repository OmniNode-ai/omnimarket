# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Regression for OMN-13719.

The declarative `onex skill pr_review` / `pr_review_bot` CLI path only supplies
`repo` / `pr_number` (plus runtime-injected `correlation_id` / `requested_at`).
It never supplies `reviewer_models` / `judge_model`, so `ReviewRequest` used to
fail validation with `reviewer_models Field required` before the orchestrator
could even fetch the diff (which it already does via `node_github_diff_effect`).

`ReviewRequest` must carry local-first defaults so the convenience CLI path
builds a valid payload while explicit callers can still override.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from omnimarket.review.pr_review_io import ReviewRequest


@pytest.mark.unit
def test_declarative_path_payload_validates_with_local_first_defaults() -> None:
    """Mirror the exact CLI declarative-path payload (no reviewer/judge keys)."""
    # This is precisely what cli_skill builds for `pr_review --repo ... --pr-number ...`:
    # only repo/pr_number, with runtime-injected correlation_id + requested_at.
    raw = {
        "repo": "OmniNode-ai/omnimarket",
        "pr_number": 1505,
        "correlation_id": uuid.uuid4(),
        "requested_at": datetime.now(UTC),
        "dry_run": True,
    }

    request = ReviewRequest(**raw)

    assert request.reviewer_models == ["local"]
    assert request.judge_model == "local"


@pytest.mark.unit
def test_explicit_reviewer_and_judge_keys_override_defaults() -> None:
    request = ReviewRequest(
        repo="OmniNode-ai/omnimarket",
        pr_number=1505,
        correlation_id=uuid.uuid4(),
        requested_at=datetime.now(UTC),
        reviewer_models=["cheap_cloud", "local"],
        judge_model="cheap_cloud",
    )

    assert request.reviewer_models == ["cheap_cloud", "local"]
    assert request.judge_model == "cheap_cloud"
