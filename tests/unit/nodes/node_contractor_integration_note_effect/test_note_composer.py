# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for the pure integration-note composer (OMN-17277).

Every case here is a failure the note mechanism has to survive, not a
line-coverage exercise:

* dev-only vs released — a note that implies a change is available when it is
  only on the integration branch sends the validator to probe something they
  cannot reach.
* missing dod_evidence — most merges do not carry one; the note must still say
  something true rather than fall over or fabricate.
* non-contractor ticket — the default outcome for almost every merge is
  silence, and silence must be a NAMED decision, not an accident.
* duplicate — the workflow can fire twice (re-run, backfill dispatch, replay);
  the second firing must be a no-op.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from omnimarket.nodes.node_contractor_integration_note_effect.models.model_integration_note_request import (
    ModelContractorRoster,
    ModelMergedPullRequest,
    ModelTicketFacts,
)
from omnimarket.nodes.node_contractor_integration_note_effect.models.model_integration_note_result import (
    EnumNoteSkipReason,
    EnumReachability,
)
from omnimarket.nodes.node_contractor_integration_note_effect.services.note_composer import (
    compose_integration_note,
    contains_internal_reference,
    extract_authored_fields,
    extract_ticket_reference,
    note_key,
    parse_note_keys,
)
from tests.unit.nodes.node_contractor_integration_note_effect.conftest import (
    CONTRACTOR_ID,
    STAFF_ID,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# dev-only vs released
# ---------------------------------------------------------------------------


def test_dev_only_merge_carries_the_pin_recipe(
    pull_request: ModelMergedPullRequest,
    contractor_ticket: ModelTicketFacts,
    roster: ModelContractorRoster,
) -> None:
    decision = compose_integration_note(
        pull_request=pull_request,
        ticket=contractor_ticket,
        roster=roster,
        release_tags=(),
        existing_note_keys=(),
    )

    assert decision.should_post is True
    assert decision.reachability is EnumReachability.DEV_ONLY
    assert "Not in a released tag yet" in decision.note_body
    assert (
        'uv pip install "git+https://github.com/OmniNode-ai/omnibase_infra'
        f'@{pull_request.merge_sha}"' in decision.note_body
    )


def test_released_merge_names_the_tag_and_omits_the_pin(
    pull_request: ModelMergedPullRequest,
    contractor_ticket: ModelTicketFacts,
    roster: ModelContractorRoster,
) -> None:
    decision = compose_integration_note(
        pull_request=pull_request,
        ticket=contractor_ticket,
        roster=roster,
        release_tags=("v0.9.2", "v0.9.1"),
        existing_note_keys=(),
    )

    assert decision.reachability is EnumReachability.RELEASED
    # Sorted, so the EARLIEST tag containing the commit is the one named as the
    # first release you can install to get it.
    assert "First tag containing this commit: v0.9.1." in decision.note_body
    assert "also in: v0.9.2" in decision.note_body
    assert "uv pip install" not in decision.note_body


# ---------------------------------------------------------------------------
# missing dod_evidence
# ---------------------------------------------------------------------------


def test_missing_dod_evidence_falls_back_to_body_prose(
    pull_request: ModelMergedPullRequest,
    contractor_ticket: ModelTicketFacts,
    roster: ModelContractorRoster,
) -> None:
    decision = compose_integration_note(
        pull_request=pull_request,
        ticket=contractor_ticket,
        roster=roster,
        release_tags=(),
        existing_note_keys=(),
    )

    assert decision.should_post is True
    assert "hardcoded default overlay path" in decision.note_body
    assert decision.redacted_fields == ()


def test_dod_evidence_section_wins_over_body_prose(
    pull_request: ModelMergedPullRequest,
    contractor_ticket: ModelTicketFacts,
    roster: ModelContractorRoster,
) -> None:
    body = (
        "## Summary\n\nSome generic prose nobody should quote.\n\n"
        "## DoD evidence\n\nLane overlay now resolves from the lane's own pin.\n"
    )
    decision = compose_integration_note(
        pull_request=pull_request.model_copy(update={"body": body}),
        ticket=contractor_ticket,
        roster=roster,
        release_tags=(),
        existing_note_keys=(),
    )

    assert "Lane overlay now resolves from the lane's own pin." in decision.note_body
    assert "generic prose" not in decision.note_body


def test_empty_body_falls_back_to_the_pr_title(
    pull_request: ModelMergedPullRequest,
    contractor_ticket: ModelTicketFacts,
    roster: ModelContractorRoster,
) -> None:
    decision = compose_integration_note(
        pull_request=pull_request.model_copy(update={"body": ""}),
        ticket=contractor_ticket,
        roster=roster,
        release_tags=(),
        existing_note_keys=(),
    )

    assert decision.should_post is True
    assert pull_request.title in decision.note_body


def test_absent_probe_is_named_not_invented(
    pull_request: ModelMergedPullRequest,
    contractor_ticket: ModelTicketFacts,
    roster: ModelContractorRoster,
) -> None:
    decision = compose_integration_note(
        pull_request=pull_request,
        ticket=contractor_ticket,
        roster=roster,
        release_tags=(),
        existing_note_keys=(),
    )

    assert "Not supplied by the merging lane." in decision.note_body
    # And the surfaces line falls back to the contractor's configured rows,
    # explicitly labelled as un-narrowed rather than asserted.
    assert "Not narrowed by the merging lane" in decision.note_body
    assert "C1, C4" in decision.note_body


def test_authored_block_supplies_probe_and_expectation(
    pull_request: ModelMergedPullRequest,
    contractor_ticket: ModelTicketFacts,
    roster: ModelContractorRoster,
) -> None:
    body = (
        "## Integration note\n"
        "What it means for your surfaces: C4 only — the lane boots instead of "
        "crash-looping.\n"
        "Probe to run: curl -sf http://localhost:58085/health\n"
        "Pass expectation: HTTP 200 with status ok.\n\n"
        "## Other section\nIrrelevant.\n"
    )
    decision = compose_integration_note(
        pull_request=pull_request.model_copy(update={"body": body}),
        ticket=contractor_ticket,
        roster=roster,
        release_tags=(),
        existing_note_keys=(),
    )

    assert "curl -sf http://localhost:58085/health" in decision.note_body
    assert "HTTP 200 with status ok." in decision.note_body
    assert "C4 only — the lane boots instead of crash-looping." in decision.note_body
    assert "Not supplied by the merging lane." not in decision.note_body
    assert "Irrelevant." not in decision.note_body


# ---------------------------------------------------------------------------
# non-contractor tickets are ignored, with a named reason
# ---------------------------------------------------------------------------


def test_ticket_assigned_to_staff_is_ignored(
    pull_request: ModelMergedPullRequest,
    contractor_ticket: ModelTicketFacts,
    roster: ModelContractorRoster,
) -> None:
    decision = compose_integration_note(
        pull_request=pull_request,
        ticket=contractor_ticket.model_copy(
            update={"assignee_linear_user_id": STAFF_ID}
        ),
        roster=roster,
        release_tags=(),
        existing_note_keys=(),
    )

    assert decision.should_post is False
    assert decision.skip_reason is EnumNoteSkipReason.ASSIGNEE_NOT_CONTRACTOR
    assert decision.note_body == ""


def test_unassigned_ticket_is_distinguished_from_non_contractor(
    pull_request: ModelMergedPullRequest,
    contractor_ticket: ModelTicketFacts,
    roster: ModelContractorRoster,
) -> None:
    decision = compose_integration_note(
        pull_request=pull_request,
        ticket=contractor_ticket.model_copy(update={"assignee_linear_user_id": None}),
        roster=roster,
        release_tags=(),
        existing_note_keys=(),
    )

    assert decision.skip_reason is EnumNoteSkipReason.TICKET_UNASSIGNED


def test_pr_without_a_ticket_reference_is_named_as_such(
    pull_request: ModelMergedPullRequest, roster: ModelContractorRoster
) -> None:
    decision = compose_integration_note(
        pull_request=pull_request.model_copy(
            update={"title": "chore: bump deps", "body": ""}
        ),
        ticket=None,
        roster=roster,
        release_tags=(),
        existing_note_keys=(),
    )

    assert decision.skip_reason is EnumNoteSkipReason.NO_TICKET_REFERENCE


def test_cited_ticket_that_does_not_resolve_is_not_silent(
    pull_request: ModelMergedPullRequest, roster: ModelContractorRoster
) -> None:
    decision = compose_integration_note(
        pull_request=pull_request,
        ticket=None,
        roster=roster,
        release_tags=(),
        existing_note_keys=(),
    )

    assert decision.skip_reason is EnumNoteSkipReason.TICKET_NOT_FOUND


def test_empty_roster_posts_nothing(
    pull_request: ModelMergedPullRequest,
    contractor_ticket: ModelTicketFacts,
    roster: ModelContractorRoster,
) -> None:
    decision = compose_integration_note(
        pull_request=pull_request,
        ticket=contractor_ticket,
        roster=roster.model_copy(update={"contractors": ()}),
        release_tags=(),
        existing_note_keys=(),
    )

    assert decision.should_post is False
    assert decision.skip_reason is EnumNoteSkipReason.ASSIGNEE_NOT_CONTRACTOR


# ---------------------------------------------------------------------------
# duplicate suppression
# ---------------------------------------------------------------------------


def test_second_firing_for_the_same_pr_is_suppressed(
    pull_request: ModelMergedPullRequest,
    contractor_ticket: ModelTicketFacts,
    roster: ModelContractorRoster,
) -> None:
    first = compose_integration_note(
        pull_request=pull_request,
        ticket=contractor_ticket,
        roster=roster,
        release_tags=(),
        existing_note_keys=(),
    )
    assert first.should_post is True

    # The posted note carries its own key; reading it back is the whole
    # idempotency mechanism — no side table, the ticket IS the record.
    keys_on_ticket = parse_note_keys([first.note_body])
    assert keys_on_ticket == (note_key(pull_request),)

    second = compose_integration_note(
        pull_request=pull_request,
        ticket=contractor_ticket,
        roster=roster,
        release_tags=(),
        existing_note_keys=keys_on_ticket,
    )

    assert second.should_post is False
    assert second.skip_reason is EnumNoteSkipReason.ALREADY_POSTED
    assert second.note_body == ""


def test_a_different_pr_on_the_same_ticket_still_posts(
    pull_request: ModelMergedPullRequest,
    contractor_ticket: ModelTicketFacts,
    roster: ModelContractorRoster,
) -> None:
    decision = compose_integration_note(
        pull_request=pull_request.model_copy(update={"number": 3121}),
        ticket=contractor_ticket,
        roster=roster,
        release_tags=(),
        existing_note_keys=("OmniNode-ai/omnibase_infra#3120",),
    )

    assert decision.should_post is True
    assert decision.note_key == "OmniNode-ai/omnibase_infra#3121"


def test_parse_note_keys_ignores_prose_that_merely_mentions_a_pr() -> None:
    assert parse_note_keys(["see OmniNode-ai/omnibase_infra#3120 for context"]) == ()


# ---------------------------------------------------------------------------
# internal-reference refusal
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "run it from /Users/someone/Code/omni_home",  # test-literal-ok: fixture proving the composer refuses an operator path
        "see $OMNI_HOME/omni_worktrees/OMN-1/x",
        "lane=omn17277-integration-note claimed this",
        "probe 10.0.0.5:8085",
        "session_id 9787a4a3 holds the receipt",
    ],
)
def test_internal_references_are_detected(text: str) -> None:
    assert contains_internal_reference(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "curl -sf http://localhost:58085/health",
        "the renderer no longer defaults the overlay path",
        "install the tagged release and re-run the CLI",
    ],
)
def test_recipient_actionable_text_is_not_flagged(text: str) -> None:
    assert contains_internal_reference(text) is False


def test_dirty_source_is_withheld_and_named(
    pull_request: ModelMergedPullRequest,
    contractor_ticket: ModelTicketFacts,
    roster: ModelContractorRoster,
) -> None:
    body = (
        "## Integration note\n"
        "Probe to run: bash /Users/someone/Code/omni_home/scripts/probe.sh\n"  # test-literal-ok: fixture proving a dirty probe is withheld
    )
    decision = compose_integration_note(
        pull_request=pull_request.model_copy(update={"body": body}),
        ticket=contractor_ticket,
        roster=roster,
        release_tags=(),
        existing_note_keys=(),
    )

    # Asserted through the composer's own detector rather than a literal path
    # substring: the property under test is "nothing internal survived", and a
    # substring check would keep passing if the withholding rule were narrowed.
    assert contains_internal_reference(decision.note_body) is False
    assert "probe" in decision.redacted_fields
    assert "Withheld:" in decision.note_body


def test_composed_note_never_leaks_an_internal_reference(
    pull_request: ModelMergedPullRequest,
    contractor_ticket: ModelTicketFacts,
    roster: ModelContractorRoster,
) -> None:
    body = (
        "Fixed under lane=omn17150 in $OMNI_HOME/omni_worktrees/OMN-17150.\n\n"
        "## DoD evidence\n\nThe lane overlay resolves from its own pin now.\n"
    )
    decision = compose_integration_note(
        pull_request=pull_request.model_copy(update={"body": body}),
        ticket=contractor_ticket,
        roster=roster,
        release_tags=(),
        existing_note_keys=(),
    )

    assert contains_internal_reference(decision.note_body) is False
    assert "The lane overlay resolves from its own pin now." in decision.note_body


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def test_title_reference_beats_a_body_reference() -> None:
    pr = ModelMergedPullRequest(
        repo="OmniNode-ai/omnimarket",
        number=1,
        title="fix(OMN-100): thing",
        body="supersedes OMN-999",
        merge_sha="abcdef1234567",
        merged_at=datetime(2026, 9, 1, tzinfo=UTC),
        base_ref="dev",
        html_url="https://example.invalid/pr/1",
    )
    assert extract_ticket_reference(pr) == "OMN-100"


def test_authored_block_absent_returns_empty() -> None:
    assert extract_authored_fields("## Summary\nno note block here\n") == {}


def test_contractor_id_constant_matches_the_shipped_roster() -> None:
    """Guards the fixture against silently drifting from the real roster."""
    assert CONTRACTOR_ID == "df034ef3-16f7-40d8-a138-1bac1d254cbf"
