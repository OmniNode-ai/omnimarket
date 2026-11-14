# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The standard ticket contract rules of dod-verify (OMN-23771, D2 to D4 of the standard-tickets plan).

The ticket and its definition of done travel in the pull request as
``contracts/<id>.yaml``. With a cutover in the node's overlay, the complete head
contract is held to the core ``ModelTicketContract`` and, for a ticket at or above the
cutover, to the standard form: schema 1.1.0, a fingerprint that recomputes, and the
repository being verified. A ticket below the cutover is recorded ``grandfathered``,
never passed silently. No rule reads the network.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import Any

from omnibase_core.models.ticket.model_ticket_contract import ModelTicketContract
from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_node_overlay,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_state import (
    EnumEvidenceCheckStatus,
    ModelEvidenceCheckResult,
)
from omnimarket.nodes.node_dod_verify.models.model_standard_ticket_cutover import (
    ModelStandardTicketCutover,
)

NODE_NAME = "node_dod_verify"
CUTOVER_DATE_FIELD = "standard_ticket_cutover_date"
CUTOVER_FIRST_TICKET_FIELD = "standard_ticket_first_ticket_number"

CONTRACT_SCHEMA_INVALID = "contract_schema_invalid"
CONTRACT_NOT_SYNCED = "contract_not_synced"
TICKET_NOT_STANDARD = "ticket_not_standard"
CONTRACT_IN_SECOND_REPOSITORY = "contract_in_second_repository"
GRANDFATHERED = "grandfathered"
CUTOVER_UNREADABLE = "standard_ticket_cutover_unreadable"

STANDARD_EVIDENCE_ID = "contract_standard"

_STANDARD_SCHEMA = (1, 1, 0)
_TICKET_NUMBER = re.compile(r"OMN-([0-9]+)")


def load_cutover(
    environ: Mapping[str, str] | None = None,
) -> ModelStandardTicketCutover | None:
    """The overlay's cutover, or ``None`` when no overlay names one.

    A present overlay that names only one of the two values, or one that does not
    parse, raises :class:`NodeOverlayError`: a broken overlay must not read as an
    absent one.
    """
    fields = load_node_overlay(
        NODE_NAME,
        fields=frozenset({CUTOVER_DATE_FIELD, CUTOVER_FIRST_TICKET_FIELD}),
        environ=environ,
    )
    if not fields:
        return None
    missing = sorted({CUTOVER_DATE_FIELD, CUTOVER_FIRST_TICKET_FIELD} - set(fields))
    if missing:
        raise NodeOverlayError(
            f"the {NODE_NAME} overlay must name both cutover values; missing {missing}"
        )
    try:
        return ModelStandardTicketCutover(
            cutover_date=date.fromisoformat(fields[CUTOVER_DATE_FIELD]),
            first_ticket_number=int(fields[CUTOVER_FIRST_TICKET_FIELD]),
        )
    except ValueError as exc:
        raise NodeOverlayError(
            f"the {NODE_NAME} overlay cutover values do not parse: {exc}"
        ) from None


def is_grandfathered(ticket_id: str, cutover: ModelStandardTicketCutover) -> bool:
    """True when the ticket's number is below the cutover's first ticket number.

    An id that carries no ``OMN-<digits>`` number is never grandfathered.
    """
    match = _TICKET_NUMBER.fullmatch(ticket_id)
    return match is not None and int(match.group(1)) < cutover.first_ticket_number


def verified_repository(
    contract_path: Path | None,
    subject_repository: str | None,
    environ: Mapping[str, str] | None = None,
) -> str | None:
    """The repository whose contract is being verified, as a bare name, or ``None``.

    The contract's git remote when it has one, else the checkout directory above its
    ``contracts/`` directory, else the ``REPO`` setting the verifier already reads.
    """
    if subject_repository:
        return subject_repository.rsplit("/", 1)[-1]
    if contract_path is not None and contract_path.resolve().parent.name == "contracts":
        return contract_path.resolve().parent.parent.name
    env = os.environ if environ is None else environ
    return env.get("REPO", "").rsplit("/", 1)[-1] or None


def _refusal(code: str, ticket_id: str, message: str) -> ModelEvidenceCheckResult:
    return ModelEvidenceCheckResult(
        evidence_id="contract",
        description=f"{code}: {ticket_id}",
        status=EnumEvidenceCheckStatus.FAILED,
        message=f"{code}: {message}",
    )


def cutover_unreadable_refusal(
    ticket_id: str, error: NodeOverlayError
) -> ModelEvidenceCheckResult:
    return _refusal(CUTOVER_UNREADABLE, ticket_id, str(error))


def schema_refusal(ticket_id: str, reason: str) -> ModelEvidenceCheckResult:
    return _refusal(CONTRACT_SCHEMA_INVALID, ticket_id, reason)


def grandfathered_record(
    ticket_id: str, cutover: ModelStandardTicketCutover
) -> ModelEvidenceCheckResult:
    """The recorded verdict for a ticket below the cutover.

    Carried as a disposition record: it appears in the verdict's checks and is left
    out of every verdict count, so it neither proves anything nor blocks anything.
    """
    return ModelEvidenceCheckResult(
        evidence_id=STANDARD_EVIDENCE_ID,
        description=f"{GRANDFATHERED}: {ticket_id}",
        status=EnumEvidenceCheckStatus.VERIFIED,
        message=(
            f"{GRANDFATHERED}: {ticket_id} is numbered below the standard-ticket "
            f"cutover (first ticket number {cutover.first_ticket_number}, date "
            f"{cutover.cutover_date.isoformat()}); the standard form, fingerprint "
            "and repository checks do not apply to it."
        ),
        is_disposition=True,
    )


def _schema_errors(exc: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(part) for part in error['loc']) or 'contract'}: {error['msg']}"
        for error in exc.errors()
    )


def standard_ticket_refusals(
    ticket_id: str,
    full_contract: Mapping[str, Any],
    cutover: ModelStandardTicketCutover,
    repository: str | None,
) -> list[ModelEvidenceCheckResult]:
    """Every refusal the complete head contract earns; empty when it is accepted.

    A contract the core model refuses stops there. Past that, a ticket below the
    cutover is accepted; one at or above it must be schema 1.1.0, carry a fingerprint
    equal to the hash of its own content, and name the repository being verified.
    """
    try:
        contract = ModelTicketContract.model_validate(dict(full_contract))
    except ValidationError as exc:
        return [schema_refusal(ticket_id, _schema_errors(exc))]
    if is_grandfathered(ticket_id, cutover):
        return []

    refusals: list[ModelEvidenceCheckResult] = []
    schema = tuple(int(part) for part in contract.schema_version.split("."))
    if schema < _STANDARD_SCHEMA:
        refusals.append(
            _refusal(
                TICKET_NOT_STANDARD,
                ticket_id,
                f"{ticket_id} is numbered at or above the standard-ticket cutover "
                f"(first ticket number {cutover.first_ticket_number}) but its contract "
                f"is schema {contract.schema_version}; a ticket at or above the cutover "
                "carries a schema 1.1.0 contract written by the ticket node.",
            )
        )
        return refusals

    stored = contract.contract_fingerprint
    computed = contract.compute_fingerprint()
    if stored != computed:
        refusals.append(
            _refusal(
                CONTRACT_NOT_SYNCED,
                ticket_id,
                f"the contract's fingerprint is {stored or 'missing'} but its content "
                f"hashes to {computed}; it was changed after its fingerprint was "
                "written. Run `onex ticket sync` to write the ticket and the file "
                "together.",
            )
        )
    named = (contract.repository or "").strip().rsplit("/", 1)[-1]
    if repository is None or named != repository:
        refusals.append(
            _refusal(
                CONTRACT_IN_SECOND_REPOSITORY,
                ticket_id,
                f"the contract names repository {contract.repository!r}, but the "
                f"repository being verified is {repository or 'not determinable'}; "
                "one ticket belongs to one repository, so split the work into one "
                "child ticket per repository.",
            )
        )
    return refusals


__all__: list[str] = [
    "CONTRACT_IN_SECOND_REPOSITORY",
    "CONTRACT_NOT_SYNCED",
    "CONTRACT_SCHEMA_INVALID",
    "CUTOVER_DATE_FIELD",
    "CUTOVER_FIRST_TICKET_FIELD",
    "GRANDFATHERED",
    "NODE_NAME",
    "STANDARD_EVIDENCE_ID",
    "TICKET_NOT_STANDARD",
    "cutover_unreadable_refusal",
    "grandfathered_record",
    "is_grandfathered",
    "load_cutover",
    "schema_refusal",
    "standard_ticket_refusals",
    "verified_repository",
]
