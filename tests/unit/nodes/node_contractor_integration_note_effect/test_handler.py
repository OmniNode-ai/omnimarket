# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler tests for the contractor integration-note effect (OMN-17277).

The handler owns sequencing and I/O only, so these tests assert exactly that:
what it reads, in what order, when it writes, and — the property the whole
mechanism rests on — that it never writes twice for the same merge.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_contractor_integration_note_effect.handlers.handler_contractor_integration_note import (
    HandlerContractorIntegrationNote,
)
from omnimarket.nodes.node_contractor_integration_note_effect.models.model_integration_note_request import (
    ModelContractorRoster,
    ModelIntegrationNoteRequest,
    ModelMergedPullRequest,
    ModelTicketFacts,
)
from omnimarket.nodes.node_contractor_integration_note_effect.models.model_integration_note_result import (
    EnumNoteSkipReason,
)
from omnimarket.nodes.node_contractor_integration_note_effect.services.note_composer import (
    parse_note_keys,
)

pytestmark = pytest.mark.unit


class FakeLinear:
    """In-memory stand-in for the Linear boundary.

    Comments are stored, so a second handle() call reads back exactly what the
    first one wrote — which is the real idempotency path, not a simulated one.
    """

    def __init__(self, ticket: ModelTicketFacts | None) -> None:
        self._ticket = ticket
        self.comments: list[str] = []
        self.fetch_calls: list[str] = []

    def fetch_ticket(self, identifier: str) -> ModelTicketFacts | None:
        self.fetch_calls.append(identifier)
        return self._ticket

    def existing_note_keys(self, issue_id: str) -> tuple[str, ...]:
        return parse_note_keys(self.comments)

    def post_note(self, issue_id: str, body: str) -> None:
        self.comments.append(body)


class FakeReleases:
    def __init__(self, tags: tuple[str, ...] = ()) -> None:
        self._tags = tags
        self.calls: list[str] = []

    def tags_containing(self, merge_sha: str) -> tuple[str, ...]:
        self.calls.append(merge_sha)
        return self._tags


def _request(
    pull_request: ModelMergedPullRequest,
    roster: ModelContractorRoster,
    *,
    dry_run: bool = False,
) -> ModelIntegrationNoteRequest:
    return ModelIntegrationNoteRequest(
        pull_request=pull_request,
        roster=roster,
        checkout_path=Path("/nonexistent-checkout"),
        dry_run=dry_run,
    )


def test_handler_posts_once_and_suppresses_the_replay(
    pull_request: ModelMergedPullRequest,
    contractor_ticket: ModelTicketFacts,
    roster: ModelContractorRoster,
) -> None:
    linear = FakeLinear(contractor_ticket)
    handler = HandlerContractorIntegrationNote(linear, FakeReleases())

    first = handler.handle(_request(pull_request, roster))
    assert first.posted is True
    assert len(linear.comments) == 1

    second = handler.handle(_request(pull_request, roster))
    assert second.posted is False
    assert second.decision.skip_reason is EnumNoteSkipReason.ALREADY_POSTED
    assert len(linear.comments) == 1, "the replay must not write a second comment"


def test_dry_run_composes_without_writing(
    pull_request: ModelMergedPullRequest,
    contractor_ticket: ModelTicketFacts,
    roster: ModelContractorRoster,
) -> None:
    linear = FakeLinear(contractor_ticket)
    result = HandlerContractorIntegrationNote(linear, FakeReleases()).handle(
        _request(pull_request, roster, dry_run=True)
    )

    assert result.decision.should_post is True
    assert result.posted is False
    assert result.dry_run is True
    assert linear.comments == []


def test_no_ticket_reference_skips_both_reads(
    pull_request: ModelMergedPullRequest, roster: ModelContractorRoster
) -> None:
    """A merge citing no ticket costs nothing — not even a Linear round trip."""
    linear = FakeLinear(None)
    releases = FakeReleases()
    result = HandlerContractorIntegrationNote(linear, releases).handle(
        _request(
            pull_request.model_copy(update={"title": "chore: bump deps", "body": ""}),
            roster,
        )
    )

    assert result.decision.skip_reason is EnumNoteSkipReason.NO_TICKET_REFERENCE
    assert linear.fetch_calls == []
    assert releases.calls == []


def test_release_probe_runs_only_after_the_ticket_resolves(
    pull_request: ModelMergedPullRequest,
    contractor_ticket: ModelTicketFacts,
    roster: ModelContractorRoster,
) -> None:
    unresolved = FakeReleases()
    HandlerContractorIntegrationNote(FakeLinear(None), unresolved).handle(
        _request(pull_request, roster)
    )
    assert unresolved.calls == []

    resolved = FakeReleases(("v1.0.0",))
    HandlerContractorIntegrationNote(FakeLinear(contractor_ticket), resolved).handle(
        _request(pull_request, roster)
    )
    assert resolved.calls == [pull_request.merge_sha]


def test_result_carries_the_merge_identity(
    pull_request: ModelMergedPullRequest,
    contractor_ticket: ModelTicketFacts,
    roster: ModelContractorRoster,
) -> None:
    result = HandlerContractorIntegrationNote(
        FakeLinear(contractor_ticket), FakeReleases()
    ).handle(_request(pull_request, roster))

    assert result.repo == pull_request.repo
    assert result.pr_number == pull_request.number
    assert result.decision.note_key == f"{pull_request.repo}#{pull_request.number}"
