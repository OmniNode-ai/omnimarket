# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerContractorIntegrationNote — canonical definition-B effect handler.

``handle(request: ModelIntegrationNoteRequest) -> ModelIntegrationNoteResult``.
Typed payload in, typed payload out; no event envelope, no handler-output
wrapper. The runtime's shared local adapter does the envelope work.

The handler owns only sequencing and I/O. Every judgement — is a note owed, what
does it say, has it already been delivered — lives in the pure composer, so the
decision is provable without a Linear key.

Related:
    - OMN-17277: integration note (WS2)
    - OMN-17274: Lakshman customer-plane validation charter (epic)
"""

from __future__ import annotations

import logging

from omnimarket.nodes.node_contractor_integration_note_effect.models.model_integration_note_request import (
    ModelIntegrationNoteRequest,
)
from omnimarket.nodes.node_contractor_integration_note_effect.models.model_integration_note_result import (
    ModelIntegrationNoteResult,
)
from omnimarket.nodes.node_contractor_integration_note_effect.services.adapters import (
    GitReleaseStateProbe,
    LinearGraphqlNoteBoundary,
    ProtocolLinearNoteBoundary,
    ProtocolReleaseStateProbe,
    resolve_linear_api_key,
)
from omnimarket.nodes.node_contractor_integration_note_effect.services.note_composer import (
    compose_integration_note,
    extract_ticket_reference,
)

logger = logging.getLogger(__name__)


class HandlerContractorIntegrationNote:
    """Post one integration note per merge that a contractor's ticket owns."""

    def __init__(
        self,
        linear: ProtocolLinearNoteBoundary | None = None,
        releases: ProtocolReleaseStateProbe | None = None,
    ) -> None:
        """Both boundaries are injectable, and default to the real ones.

        Optional rather than required so the runtime's boot resolver can
        construct this handler from the three known-injectable providers alone
        (OMN-13551): a required, default-less parameter makes the handler
        unresolvable and it is quarantined behind a boot warning nobody reads.
        The default is not a degraded stand-in — it is the live boundary, built
        from the contract-declared secret, and it fails closed if that secret is
        unset. Tests and dry runs inject fakes instead.
        """
        self._linear = linear
        self._releases = releases

    def handle(
        self, request: ModelIntegrationNoteRequest
    ) -> ModelIntegrationNoteResult:
        pull_request = request.pull_request
        linear = self._linear or LinearGraphqlNoteBoundary(resolve_linear_api_key())
        releases = self._releases or GitReleaseStateProbe(request.checkout_path)

        ticket_ref = extract_ticket_reference(pull_request)
        ticket = linear.fetch_ticket(ticket_ref) if ticket_ref else None

        # The release probe and the comment read are only worth their cost once
        # the merge is known to belong to a contractor's ticket. Ordering them
        # after the cheap resolution keeps the common case — a merge nobody is
        # waiting on — down to a single Linear read.
        release_tags: tuple[str, ...] = ()
        existing_keys: tuple[str, ...] = ()
        if ticket is not None:
            release_tags = releases.tags_containing(pull_request.merge_sha)
            existing_keys = linear.existing_note_keys(ticket.issue_id)

        decision = compose_integration_note(
            pull_request=pull_request,
            ticket=ticket,
            roster=request.roster,
            release_tags=release_tags,
            existing_note_keys=existing_keys,
        )

        posted = False
        if decision.should_post and not request.dry_run:
            assert ticket is not None  # should_post implies a resolved ticket
            linear.post_note(ticket.issue_id, decision.note_body)
            posted = True
            logger.info(
                "integration note posted: ticket=%s key=%s reachability=%s",
                decision.ticket_identifier,
                decision.note_key,
                decision.reachability,
            )
        elif not decision.should_post:
            logger.info(
                "no integration note owed: key=%s reason=%s",
                decision.note_key,
                decision.skip_reason,
            )

        return ModelIntegrationNoteResult(
            repo=pull_request.repo,
            pr_number=pull_request.number,
            decision=decision,
            posted=posted,
            dry_run=request.dry_run,
        )


__all__ = ["HandlerContractorIntegrationNote"]
