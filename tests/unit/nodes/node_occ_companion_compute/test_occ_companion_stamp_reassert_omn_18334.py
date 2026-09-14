# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18334: a description edit that drops the evidence line is repaired, not re-minted.

The mechanical ticket-closeout plan of record measured the loss: a pull-request
description is a surface a lane overwrites wholesale, with no compare-and-swap,
and it silently lost the evidence-source line on four of the last five misses --
twice after the pull request had already merged. One lost line makes every
downstream reader conclude no evidence exists.

Before this ticket the compute had exactly two answers for a product pull
request. Bound -> ``ALREADY_BOUND`` no-op. Unbound -> author a whole new
companion. Neither is right for the third case this ticket adds: a pull request
whose companion ALREADY EXISTS and whose body no longer names it. Authoring
there mints a SECOND companion for the same ticket -- a same-ticket collision
that has to be resolved by structural union afterwards -- when the repair is one
line of text pointing at the companion that is already there.

The load-bearing precondition was answered by observation before any of this was
built: a ``pull_request`` ``edited`` event on an ALREADY-MERGED pull request DOES
dispatch workflow runs (probe recorded on the ticket), so the post-merge half of
this repair is reachable and does not fall back to the replay dispatch.
"""

from __future__ import annotations

import pytest

from omnimarket.events.occ_companion import (
    EnumCompanionSuppressionCode,
    ModelOccExistingCompanion,
)
from omnimarket.nodes.node_occ_companion_compute.handlers.handler_occ_companion_compute import (
    compute_companion_plan,
)
from omnimarket.nodes.node_occ_companion_compute.models.model_occ_companion_request import (
    ModelObservedProbe,
    ModelOccCompanionRequest,
)

pytestmark = pytest.mark.unit

_REPO = "OmniNode-ai/omnimemory"
_PR = 450
_OCC_PR = 9412
_STAMP = f"Evidence-Source: OCC#{_OCC_PR}"

#: The description as a lane rewrote it: the human prose survived, the evidence
#: block did not.
_LOSSY_BODY = "Fixes the drift.\n\nSee OMN-16669 for the acceptance criteria.\n"


def _probe() -> ModelObservedProbe:
    return ModelObservedProbe(
        command=f"gh pr view {_PR} --repo {_REPO} --json number,state",
        stdout=f'{{"number":{_PR},"state":"OPEN"}}',
        exit_code=0,
    )


def _existing() -> ModelOccExistingCompanion:
    return ModelOccExistingCompanion(
        pr_number=_OCC_PR,
        state="open",
        merged=False,
        head_branch=f"auto/{_REPO.replace('/', '-').lower()}-pr-{_PR}-occ-autobind",
    )


def _request(**overrides: object) -> ModelOccCompanionRequest:
    """A request that AUTHORS by default, so every branch below is load-bearing."""
    base: dict[str, object] = {
        "repo": _REPO,
        "pr_number": _PR,
        "pr_head_sha": "a" * 40,
        "pr_title": "docs(OMN-16669): correct the settings docstring default",
        "pr_body": _LOSSY_BODY,
        "pr_state": "open",
        "pr_head_ref": "jonah/omn-16669-fix",
        "changed_files": ("src/omnimemory/settings.py", "README.md"),
        "diff_total_lines": 9,
        "run_timestamp": "2026-09-13T19:00:00Z",
        "product_probe": _probe(),
    }
    base.update(overrides)
    return ModelOccCompanionRequest(**base)  # type: ignore[arg-type]


def _stamp_lines(body: str) -> list[str]:
    return [
        line
        for line in body.splitlines()
        if line.strip().lower().startswith("evidence-source:")
    ]


# --------------------------------------------------------------------------
# AC2 -- a description edit that drops the line triggers a re-assert
# --------------------------------------------------------------------------


def test_dropped_line_with_an_existing_companion_re_asserts() -> None:
    """RED before this ticket: the plan authored a SECOND companion instead."""
    plan = compute_companion_plan(_request(existing_companion=_existing()))

    assert plan.reassert_stamp is True
    assert plan.reassert_occ_pr_number == _OCC_PR
    assert plan.evidence_source_occ_pr == _OCC_PR
    assert plan.no_op is False
    assert plan.companion_files == (), (
        "the companion already exists; authoring a second one for the same "
        "ticket is the same-ticket collision this branch exists to avoid"
    )
    assert _stamp_lines(plan.product_body_stamped) == [_STAMP]


def test_re_assert_preserves_the_human_prose() -> None:
    plan = compute_companion_plan(_request(existing_companion=_existing()))
    assert "Fixes the drift." in plan.product_body_stamped
    assert "See OMN-16669 for the acceptance criteria." in plan.product_body_stamped


def test_re_assert_names_the_ticket_set_it_repaired() -> None:
    plan = compute_companion_plan(_request(existing_companion=_existing()))
    assert plan.tickets == ("OMN-16669",)


def test_a_merged_product_pr_is_still_repaired() -> None:
    """The measured losses were twice on a MERGED pull request.

    The merged-unbound branch reports a permanently lost record. That is the
    right answer when no companion was ever minted, and the wrong one when the
    companion exists and only the line naming it is gone -- so the re-assert
    branch is ahead of it.
    """
    plan = compute_companion_plan(
        _request(pr_state="closed", pr_merged=True, existing_companion=_existing())
    )
    assert plan.reassert_stamp is True
    assert plan.reassert_occ_pr_number == _OCC_PR
    assert plan.suppression is None


# --------------------------------------------------------------------------
# AC3 -- re-entrant, and exactly one line
# --------------------------------------------------------------------------


def test_running_twice_leaves_exactly_one_line() -> None:
    first = compute_companion_plan(_request(existing_companion=_existing()))
    repaired = first.product_body_stamped
    assert _stamp_lines(repaired) == [_STAMP]

    second = compute_companion_plan(
        _request(pr_body=repaired, existing_companion=_existing())
    )
    assert second.no_op is True
    assert second.reassert_stamp is False
    assert second.suppression is not None
    assert second.suppression.code is EnumCompanionSuppressionCode.ALREADY_BOUND
    assert _stamp_lines(repaired) == [_STAMP], (
        "the second run must not append a second line"
    )


# --------------------------------------------------------------------------
# Positive controls -- the two answers that already existed are unchanged
# --------------------------------------------------------------------------


def test_no_existing_companion_still_authors() -> None:
    plan = compute_companion_plan(_request())
    assert plan.reassert_stamp is False
    assert plan.no_op is False
    assert plan.companion_files, "the born path must still mint a companion"


def test_a_body_that_still_carries_the_line_is_a_no_op() -> None:
    bound = f"{_LOSSY_BODY}\n{_STAMP}\n"
    plan = compute_companion_plan(
        _request(pr_body=bound, existing_companion=_existing())
    )
    assert plan.no_op is True
    assert plan.reassert_stamp is False
    assert plan.suppression is not None
    assert plan.suppression.code is EnumCompanionSuppressionCode.ALREADY_BOUND


def test_no_ticket_is_still_a_no_op_even_with_an_existing_companion() -> None:
    """A repair needs a ticket to render the evidence block against."""
    plan = compute_companion_plan(
        _request(
            pr_title="chore: tidy",
            pr_body="No ticket anywhere.",
            existing_companion=_existing(),
        )
    )
    assert plan.no_op is True
    assert plan.reassert_stamp is False
    assert plan.suppression is not None
    assert plan.suppression.code is EnumCompanionSuppressionCode.NO_TICKET


def test_an_occ_internal_pr_is_never_repaired() -> None:
    """The OCC repo's own pull requests carry no companion of their own."""
    plan = compute_companion_plan(
        _request(
            repo="OmniNode-ai/onex_change_control",
            existing_companion=_existing(),
        )
    )
    assert plan.reassert_stamp is False
    assert plan.no_op is True
    assert plan.suppression is not None
    assert plan.suppression.code is EnumCompanionSuppressionCode.OCC_SELF_COMPANION
