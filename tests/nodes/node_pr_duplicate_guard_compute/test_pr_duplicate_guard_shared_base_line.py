# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Two pull requests that replace one base line with different content are not one fix (OMN-19998).

The recorded case: a rendered database catalog holds a ``sha256:`` fingerprint line at line 493 of nine files. One
pull request added a table, another added four eval-table grants; each rewrote that one line to its own value and
added its own list rows. Both diffs carry the same removed line, and under a shared ticket that one shared digest
was enough to read them as the same change. The guard now compares the added lines when both sides add any.
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

NOW = datetime(2026, 11, 15, 21, 0, tzinfo=UTC)
CATALOG = "docker/catalog/database-topology/local.yaml"
OLD_FINGERPRINT = "6ba08450e54a3b8af18c0b30a1f487cf1697c890981c95eda6097c3ac76ce0e5"


def catalog_diff(*, rows: tuple[str, ...], row_line: int, new_fingerprint: str) -> str:
    """A ``git diff -U0`` of the catalog: list rows inserted, then the fingerprint line replaced at line 493."""
    inserted = "".join(f"+              - {row}\n" for row in rows)
    return (
        f"diff --git a/{CATALOG} b/{CATALOG}\n"
        f"--- a/{CATALOG}\n"
        f"+++ b/{CATALOG}\n"
        f"@@ -{row_line},0 +{row_line + 1},{len(rows)} @@ databases:\n"
        f"{inserted}"
        "@@ -493 +497 @@ source:\n"
        f"-  sha256: {OLD_FINGERPRINT}\n"
        f"+  sha256: {new_fingerprint}\n"
    )


STATE_PANEL = catalog_diff(
    rows=("work_state_panel_state",),
    row_line=428,
    new_fingerprint="2fb876ad020d48a98f7b94d47851dc9bf75db5e9b6b0376ea17bf149292ffd1c",
)
EVAL_GRANTS = catalog_diff(
    rows=("eval_item_scores", "eval_item_traces", "eval_run_closes", "eval_run_rates"),
    row_line=310,
    new_fingerprint="cfc84fba73b206ecb3d10150abd5e6948cd0cffdea8c49f25e9eb298de9b6400",
)


def open_pr(diff: str) -> ModelOpenPr:
    return ModelOpenPr(
        number=5998,
        head_ref="omn-19998-work-state-panel-state-table",
        base="dev",
        title="OMN-24528 work_state_panel_state table and runtime grant for the board panel row",
        touch=parse_diff(diff),
    )


def run(diff: str, open_prs: tuple[ModelOpenPr, ...]) -> ModelPrDuplicateGuardResult:
    return HandlerPrDuplicateGuard().handle(
        ModelPrDuplicateGuardRequest.model_validate(
            {
                "now": NOW,
                "repo": "OmniNode-ai/omnibase_infra",
                "head_ref": "omn-19998-eval-table-grants",
                "base": "dev",
                "title": "OMN-19998: eval-table grants for tenant_projection_writer",
                "touch": parse_diff(diff),
                "open_prs": open_prs,
            }
        )
    )


def test_parse_diff_separates_added_lines_from_the_shared_removed_line() -> None:
    state_panel = {f.path: f for f in parse_diff(STATE_PANEL).files}[CATALOG]
    grants = {f.path: f for f in parse_diff(EVAL_GRANTS).files}[CATALOG]

    assert set(state_panel.digest) & set(grants.digest), (
        "the removed fingerprint line is shared"
    )
    assert not set(state_panel.added_digest) & set(grants.added_digest)
    assert set(state_panel.added_digest) < set(state_panel.digest)


def test_two_pull_requests_replacing_one_catalog_line_with_different_content_pass() -> (
    None
):
    result = run(EVAL_GRANTS, (open_pr(STATE_PANEL),))

    assert result.state == "clear"
    assert result.evidence == ()
    assert any(
        f"{CATALOG}:493" in note and "different intent" in note for note in result.notes
    )


def test_a_true_duplicate_of_the_same_catalog_change_is_still_refused() -> None:
    result = run(EVAL_GRANTS, (open_pr(EVAL_GRANTS),))

    assert result.state == "adopt"
    assert result.adopt_pr == "omnibase_infra#5998"
    assert result.evidence[0].kind is EnumDuplicateEvidence.LINES


def test_two_pure_deletions_of_the_same_line_are_still_the_same_change() -> None:
    deletion = (
        f"diff --git a/{CATALOG} b/{CATALOG}\n"
        f"--- a/{CATALOG}\n"
        f"+++ b/{CATALOG}\n"
        "@@ -493 +492,0 @@ source:\n"
        f"-  sha256: {OLD_FINGERPRINT}\n"
    )
    result = run(deletion, (open_pr(deletion),))

    assert result.state == "adopt"
