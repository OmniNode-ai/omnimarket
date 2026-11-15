# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Two pull requests that replace one base line with different content are not one fix (OMN-19998).

The recorded case: a rendered database catalog holds a ``sha256:`` fingerprint line at line 493 of nine files. One
pull request added a table, another added four eval-table grants; each rewrote that one line to its own value and
added its own list rows. Both diffs carry the same removed line, and under a shared ticket that one shared digest
was enough to read them as the same change. The guard compares added lines and removed lines apart from the declared shared catalog line.
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


def test_duplicate_guard_eval_grants_one_shared_catalog_line_distinct_content_clears() -> (
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


def test_duplicate_guard_shared_removals_with_different_added_lines_refuse() -> None:
    """Two diffs that delete the same 20 lines are one change, whatever each adds in their place."""
    removed = "".join(f"-    legacy_setting_{n:02d}: enabled\n" for n in range(20))

    def diff(added: str) -> str:
        return (
            "diff --git a/src/app/config.yaml b/src/app/config.yaml\n"
            "--- a/src/app/config.yaml\n"
            "+++ b/src/app/config.yaml\n"
            "@@ -10,20 +10 @@ settings:\n"
            f"{removed}"
            f"+    {added}\n"
        )

    mine, theirs = diff("modern_setting_one: on"), diff("another_replacement: off")
    path = "src/app/config.yaml"
    new_touch = {f.path: f for f in parse_diff(mine).files}[path]
    old_touch = {f.path: f for f in parse_diff(theirs).files}[path]
    assert len(set(new_touch.removed_digest) & set(old_touch.removed_digest)) == 20
    assert not set(new_touch.added_digest) & set(old_touch.added_digest)

    result = run(mine, (open_pr(theirs),))

    assert result.state == "adopt"
    assert result.adopt_pr == "omnibase_infra#5998"
    assert result.evidence[0].kind is EnumDuplicateEvidence.LINES


def test_duplicate_guard_one_shared_removed_line_that_is_not_a_catalog_line_refuses() -> (
    None
):
    """Only the declared catalog fingerprint line is excused; any other shared removed line still counts."""

    def diff(added: str) -> str:
        return (
            "diff --git a/src/app/config.yaml b/src/app/config.yaml\n"
            "--- a/src/app/config.yaml\n"
            "+++ b/src/app/config.yaml\n"
            "@@ -10 +10 @@ settings:\n"
            "-    legacy_setting: enabled\n"
            f"+    {added}\n"
        )

    result = run(diff("first_replacement: on"), (open_pr(diff("second_one: off")),))

    assert result.state == "adopt"
