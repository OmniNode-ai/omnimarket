# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Create and certify a draft issue by its client-supplied Linear UUID."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Literal, Protocol, runtime_checkable
from uuid import UUID

from omnibase_core.models.ticket.model_ticket_contract import (
    STANDARD_FORM_SCHEMA_VERSION,
    ModelTicketContract,
)
from pydantic import AwareDatetime, BaseModel, Field, field_validator

from omnimarket.models.model_ticket_io import (
    ModelCreateTicketRequest,
    ModelCreateTicketResult,
)
from omnimarket.nodes.node_create_ticket.handlers.handler_ticket_project import (
    TicketProjectGatewayProtocol,
    clear_inherited_project,
)
from omnimarket.nodes.node_ticket_contract_render_compute import (
    NodeTicketContractRenderCompute,
)
from omnimarket.nodes.node_ticket_contract_render_compute.models import (
    EnumTicketContractRenderOperation,
    EnumTicketContractRenderOutcome,
    ModelTicketContractRenderRequest,
)

type DraftFailureReason = Literal[
    "draft_payload_conflict", "hand_edited", "id_unreadable"
]
_PREVIEW_DATE = datetime(1970, 1, 1, tzinfo=UTC)


class TicketDraftError(RuntimeError):
    """A retry or readback refusal safe to name on the bus terminal."""

    def __init__(self, reason: DraftFailureReason) -> None:
        super().__init__(reason)
        self.reason = reason


class DraftIssueCreateRefusedError(RuntimeError):
    """The create mutation was refused or had an unknown outcome."""


class ModelLinearDraftIssue(BaseModel):
    """Fields returned by both the draft create and direct UUID lookup."""

    id: UUID
    identifier: str = Field(min_length=1)
    url: str = Field(min_length=1)
    title: str
    description: str
    created_at: AwareDatetime = Field(validation_alias="createdAt")
    updated_at: str = Field(validation_alias="updatedAt", min_length=1)

    @field_validator("description", mode="before")
    @classmethod
    def _empty_description(cls, value: object) -> object:
        return "" if value is None else value


@runtime_checkable
class ProtocolLinearDraftTicketGateway(Protocol):
    def create_draft_issue(
        self,
        *,
        issue_id: UUID,
        title: str,
        description: str,
        team: str,
        parent: str,
        assignee_id: str | None,
        label_names: Sequence[str],
    ) -> ModelLinearDraftIssue: ...

    def read_draft_issue(self, issue_id: UUID) -> ModelLinearDraftIssue | None: ...

    def write_draft_description(self, issue_id: UUID, description: str) -> None: ...

    def project_gateway(self) -> TicketProjectGatewayProtocol: ...


def _normalize_description(body: str) -> str:
    # Line endings and trailing whitespace at the end of the body are transport details.
    # Keep interior whitespace, Markdown, filing lines and the fenced block significant.
    return body.replace("\r\n", "\n").rstrip()


class DraftTicketCreate:
    """One run's immutable draft inputs, used for the stub and the final rendering."""

    def __init__(self, request: ModelCreateTicketRequest, description: str) -> None:
        draft, draft_id = request.draft, request.draft_id
        if draft is None or draft_id is None:
            raise ValueError("a draft and draft id are required")
        first_line, _, rest = description.partition("\n")
        first_line = first_line.removesuffix("\r")
        if not first_line.strip() or rest.strip():
            raise ValueError("draft descriptions carry only the first-line lane stamp")

        self.draft = draft.model_copy(
            update={"repository": request.repo or draft.repository}
        )
        self.parent = request.parent or draft.parent_ticket_id
        self.team = request.team
        self.issue_id = UUID(
            bytes=hashlib.sha256(b"onex-ticket-draft\0" + draft_id.bytes).digest()[:16],
            version=4,
        )
        payload = request.model_dump(
            mode="json",
            exclude={
                "correlation_id",
                "draft_id",
                "dry_run",
                "description",
                "title",
                "parent",
                "repo",
            },
        )
        payload.update(
            {
                "parent": self.parent,
                "repository": self.draft.repository,
                "lane_stamp": first_line,
            }
        )
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode()
        ).hexdigest()
        self.stamp_prefix = f"draft-id: {draft_id} payload-sha256: "
        self.filing_lines = (first_line, self.stamp_prefix + digest)
        self.stub = "\n".join(self.filing_lines) + "\n"
        self.renderer = NodeTicketContractRenderCompute()

    def _render(
        self, identifier: str, created_at: datetime
    ) -> tuple[ModelTicketContract, str]:
        # Both fields are required by the contract schema. At initial rendering they
        # use the issue's immutable creation time so a rebuild emits identical bytes.
        contract = ModelTicketContract.model_validate(
            {
                **dict(self.draft),
                "schema_version": STANDARD_FORM_SCHEMA_VERSION,
                "ticket_id": identifier,
                "created_at": created_at,
                "updated_at": created_at,
            }
        )
        contract.contract_fingerprint = contract.compute_fingerprint()
        rendered = self.renderer.handle(
            ModelTicketContractRenderRequest(
                operation=EnumTicketContractRenderOperation.RENDER,
                contract=contract,
                filing_lines=self.filing_lines,
            )
        )
        if rendered.outcome is not EnumTicketContractRenderOutcome.RENDERED:
            raise ValueError("the draft contract could not be rendered")
        return contract, rendered.body

    def preview(self) -> ModelCreateTicketResult:
        """Let the guard judge the rendered request before the first Linear call."""
        contract, body = self._render("OMN-0", _PREVIEW_DATE)
        return ModelCreateTicketResult(
            status="dry_run",
            title=self.draft.title,
            team=self.team,
            route="draft",
            description_body=body,
            contract=contract,
            fingerprint=contract.contract_fingerprint,
            dry_run=True,
        )

    def _verify_identity(self, issue: ModelLinearDraftIssue) -> None:
        if issue.id != self.issue_id:
            raise TicketDraftError("id_unreadable")
        prefix = _normalize_description(issue.description).split("\n", 2)
        if len(prefix) < 2 or prefix[0] != self.filing_lines[0]:
            raise TicketDraftError("hand_edited")
        if prefix[1] != self.filing_lines[1]:
            reason: DraftFailureReason = (
                "draft_payload_conflict"
                if prefix[1].startswith(self.stamp_prefix)
                else "hand_edited"
            )
            raise TicketDraftError(reason)
        if issue.title.strip() != self.draft.title.strip():
            raise TicketDraftError("hand_edited")

    def _read_required(
        self, gateway: ProtocolLinearDraftTicketGateway, identifier: str | None = None
    ) -> ModelLinearDraftIssue:
        issue = gateway.read_draft_issue(self.issue_id)
        if issue is None:
            raise TicketDraftError("id_unreadable")
        self._verify_identity(issue)
        if identifier is not None and issue.identifier != identifier:
            raise TicketDraftError("hand_edited")
        return issue

    def create(
        self,
        gateway: ProtocolLinearDraftTicketGateway,
        *,
        assignee_id: str | None,
        label_names: Sequence[str],
        guard_final: Callable[[str], str],
    ) -> ModelCreateTicketResult:
        issue = gateway.read_draft_issue(self.issue_id)
        if issue is None:
            try:
                issue = gateway.create_draft_issue(
                    issue_id=self.issue_id,
                    title=self.draft.title,
                    description=self.stub,
                    team=self.team,
                    parent=self.parent,
                    assignee_id=assignee_id,
                    label_names=label_names,
                )
            except DraftIssueCreateRefusedError:
                # One lookup covers both duplicate refusal and an ambiguous create
                # that took effect. A miss ends this run without a second create.
                issue = self._read_required(gateway)
        self._verify_identity(issue)
        clear_inherited_project(gateway.project_gateway(), str(self.issue_id))
        contract, body = self._render(issue.identifier, issue.created_at)
        guard_path = guard_final(body)

        # Linear has no compare-and-set. Check immediately before the write and
        # never retry it here, so a newer hand edit is not overwritten on retry.
        readback = self._read_required(gateway, issue.identifier)
        if _normalize_description(readback.description) == _normalize_description(
            self.stub
        ):
            gateway.write_draft_description(self.issue_id, body)
            readback = self._read_required(gateway, issue.identifier)

        if _normalize_description(readback.description) != _normalize_description(body):
            raise TicketDraftError("hand_edited")
        parsed = self.renderer.handle(
            ModelTicketContractRenderRequest(
                operation=EnumTicketContractRenderOperation.PARSE,
                body=_normalize_description(readback.description),
            )
        )
        if (
            parsed.outcome is not EnumTicketContractRenderOutcome.PARSED
            or parsed.findings
            or parsed.recomputed_fingerprint != contract.contract_fingerprint
        ):
            raise TicketDraftError("hand_edited")

        return ModelCreateTicketResult(
            status="created",
            route="draft",
            ticket_id=readback.identifier,
            ticket_url=readback.url,
            title=readback.title,
            team=self.team,
            guard_path=guard_path,
            is_seam_ticket=contract.is_seam_ticket,
            interfaces_touched=[
                interface.value for interface in contract.interfaces_touched
            ],
            contract_completeness=contract.contract_completeness.value,
            description_body=readback.description,
            updated_at=readback.updated_at,
            contract=contract,
            fingerprint=contract.contract_fingerprint,
        )
