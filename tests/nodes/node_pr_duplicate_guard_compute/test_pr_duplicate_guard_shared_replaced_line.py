# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One shared replaced line is not one shared change (OMN-24648).

Two diffs that each replace the same single base line carry that line's removal in common, and nothing else. Scored
on its own, the removed side reads that lone line as a share of 1.0, so two distinct changes under different tickets
that happen to rewrite one line refuse each other. The share rule scores one share over all the changed lines of
both diffs, added and removed together, less the removed lines a deployment declares shared catalog lines for that
path, so a lone shared line weighs what it weighs among the rest (a third here, and the shared-ticket bar is
unchanged: adopt under one ticket, clear under two).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from omnimarket.models.model_pr_duplicate_guard import (
    EnumDuplicateEvidence,
    ModelOpenPr,
    ModelPrDuplicateGuardRequest,
    ModelPrDuplicateGuardResult,
)
from omnimarket.nodes.node_pr_duplicate_guard_compute.handlers.handler_pr_duplicate_guard import (
    HandlerPrDuplicateGuard,
)
from omnimarket.nodes.node_pr_duplicate_snapshot_effect.handlers.handler_pr_duplicate_snapshot import (
    parse_diff,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 15, 23, 50, tzinfo=UTC)
PATH = "src/app/settings.yaml"


def replacement(
    *, removed: tuple[str, ...], added: tuple[str, ...], first_line: int = 10
) -> str:
    """A ``git diff -U0`` of one hunk that replaces the removed lines with the added lines."""
    return (
        f"diff --git a/{PATH} b/{PATH}\n"
        f"--- a/{PATH}\n"
        f"+++ b/{PATH}\n"
        f"@@ -{first_line},{len(removed)} +{first_line},{len(added)} @@ settings:\n"
        + "".join(f"-    {line}\n" for line in removed)
        + "".join(f"+    {line}\n" for line in added)
    )


def run(
    diff: str,
    other_diff: str,
    *,
    new_ticket: str,
    other_ticket: str,
) -> ModelPrDuplicateGuardResult:
    other = ModelOpenPr(
        number=6001,
        head_ref=f"{other_ticket.lower()}-other-change",
        base="dev",
        title=f"{other_ticket}: the other change",
        touch=parse_diff(other_diff),
    )
    return HandlerPrDuplicateGuard().handle(
        ModelPrDuplicateGuardRequest.model_validate(
            {
                "now": NOW,
                "repo": "OmniNode-ai/omnimarket",
                "head_ref": f"{new_ticket.lower()}-new-change",
                "base": "dev",
                "title": f"{new_ticket}: the new change",
                "touch": parse_diff(diff),
                "open_prs": (other,),
            }
        )
    )


LONE_MINE = replacement(
    removed=("retry_limit: 3",), added=("retry_limit: 5  # raised for the batch lane",)
)
LONE_THEIRS = replacement(
    removed=("retry_limit: 3",), added=("retry_limit: 1  # lowered for the api lane",)
)


def test_duplicate_guard_shared_replaced_line_distinct_tickets_clears() -> None:
    """The reviewed probe: one shared replaced line, different added content, different tickets."""
    result = run(LONE_MINE, LONE_THEIRS, new_ticket="OMN-1001", other_ticket="OMN-1002")

    assert result.state == "clear"
    assert result.evidence == ()


def test_duplicate_guard_shared_replaced_line_with_other_distinct_content_clears() -> (
    None
):
    """One shared replaced line among otherwise distinct removed and added lines is one of seven changed lines."""
    mine = replacement(
        removed=("retry_limit: 3", "batch_window: 10"),
        added=("retry_limit: 5", "batch_size: 50"),
    )
    theirs = replacement(
        removed=("retry_limit: 3", "api_window: 20"),
        added=("retry_limit: 1", "api_size: 5"),
    )

    result = run(mine, theirs, new_ticket="OMN-1001", other_ticket="OMN-1002")

    assert result.state == "clear"
    assert result.evidence == ()


def test_duplicate_guard_shared_replaced_line_under_a_shared_ticket_keeps_the_ticket_bar() -> (
    None
):
    """Under one ticket the lower bar still reads a shared line as a part shared: the rule this fix leaves alone."""
    result = run(LONE_MINE, LONE_THEIRS, new_ticket="OMN-1001", other_ticket="OMN-1001")

    assert result.state == "adopt"
    assert result.evidence[0].kind is EnumDuplicateEvidence.LINES


def test_duplicate_guard_shared_replaced_line_pure_deletion_of_it_still_refuses() -> (
    None
):
    """Two pure deletions of the same one line have no added side to differ on: the same change."""
    deletion = replacement(removed=("retry_limit: 3",), added=())

    result = run(deletion, deletion, new_ticket="OMN-1001", other_ticket="OMN-1002")

    assert result.state == "adopt"
    assert result.evidence[0].kind is EnumDuplicateEvidence.LINES


def test_duplicate_guard_shared_removals_of_many_lines_with_different_added_lines_refuse() -> (
    None
):
    """Many shared removals are most of both changes, whatever each adds in their place."""
    removed = tuple(f"legacy_setting_{n:02d}: enabled" for n in range(8))

    result = run(
        replacement(removed=removed, added=("modern_setting: on",)),
        replacement(removed=removed, added=("another_replacement: off",)),
        new_ticket="OMN-1001",
        other_ticket="OMN-1002",
    )

    assert result.state == "adopt"
    assert result.evidence[0].kind is EnumDuplicateEvidence.LINES


def test_duplicate_guard_shared_line_among_distinct_lines_under_one_ticket_stays_clear() -> (
    None
):
    """Through parse_diff: one shared replaced line among thirty distinct lines each is 1 of 123 changed lines, below the 0.1 bar."""
    mine = replacement(
        removed=("retry_limit: 3", *(f"batch_window_{n:02d}: {n}" for n in range(30))),
        added=("retry_limit: 5", *(f"batch_size_{n:02d}: {n}" for n in range(30))),
    )
    theirs = replacement(
        removed=("retry_limit: 3", *(f"api_window_{n:02d}: {n}" for n in range(30))),
        added=("retry_limit: 1", *(f"api_size_{n:02d}: {n}" for n in range(30))),
    )
    mine_digest = {d for f in parse_diff(mine).files for d in f.digest}
    theirs_digest = {d for f in parse_diff(theirs).files for d in f.digest}
    share = len(mine_digest & theirs_digest) / len(mine_digest | theirs_digest)
    assert 0 < share < 0.1, share

    for other_ticket in ("OMN-1001", "OMN-1002"):
        result = run(mine, theirs, new_ticket="OMN-1001", other_ticket=other_ticket)

        assert result.state == "clear", other_ticket
        assert result.evidence == ()
