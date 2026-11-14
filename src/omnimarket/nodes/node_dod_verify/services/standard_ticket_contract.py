# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""dod-verify judges the ticket contract as the standard ticket form (OMN-23771).

The ticket and its definition of done travel inside the pull request, so the
verifier reads the complete contract through omnibase_core's
``ModelTicketContract`` and refuses, before any check runs:

* ``contract_schema_invalid``: the core model rejects the contract (an unknown
  key, a field it requires, a 1.1.0 rule such as the integration rule);
* ``ticket_not_standard``: a ticket at or above the cutover still carries a
  1.0.0 contract;
* ``contract_not_synced``: a 1.1.0 contract whose stored fingerprint no longer
  recomputes, so it was edited without the ticket node (D6: a plain hash that
  shows a copy changed after its hash was written, never who wrote it);
* ``contract_in_second_repository``: a ticket at or above the cutover whose
  contract's ``repository`` names a repository other than the one it is
  checked in.

A ticket below the cutover with a 1.0.0 contract is not refused: it is recorded
as ``grandfathered`` on the verdict and its runnable checks still run.

The cutover is an overlay value read from ``DOD_VERIFY_STANDARD_TICKET_CUTOVER``:
the number of the first ticket held to the standard form (set at the omnimarket
pilot). With it unset no ticket is at or above the cutover. A value that is not
a whole number is itself a refusal: a typo must not switch the rule off.

No network read: everything here is the contract text, the ticket id and the
checkout's own repository name.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from omnibase_core.models.ticket.model_ticket_contract import ModelTicketContract
from pydantic import ValidationError

from omnimarket.nodes.node_dod_verify.models.model_dod_verify_state import (
    EnumEvidenceCheckStatus,
    ModelEvidenceCheckResult,
)

CUTOVER_ENV = "DOD_VERIFY_STANDARD_TICKET_CUTOVER"
REFUSAL_SCHEMA_INVALID = "contract_schema_invalid"
REFUSAL_NOT_STANDARD = "ticket_not_standard"
REFUSAL_NOT_SYNCED = "contract_not_synced"
REFUSAL_SECOND_REPOSITORY = "contract_in_second_repository"
REFUSAL_CUTOVER_INVALID = "standard_cutover_invalid"
STANDING_GRANDFATHERED = "grandfathered"

_STANDARD_SCHEMA = (1, 1, 0)
_TICKET_NUMBER_RE = re.compile(r"^OMN-([0-9]+)$")
_MAX_DETAIL = 1200


class StandardContractCutoverError(ValueError):
    """The cutover overlay value is set and is not a whole ticket number."""


@dataclass(frozen=True)
class StandardContractJudgement:
    """What the standard-form read found: a refusal, or a standing to record."""

    refusal: ModelEvidenceCheckResult | None = None
    standing: str | None = None


def read_cutover(environ: Mapping[str, str] | None = None) -> int | None:
    """The first ticket number held to the standard form, or None when unset."""
    raw = (os.environ if environ is None else environ).get(CUTOVER_ENV, "").strip()
    if not raw:
        return None
    if not raw.isascii() or not raw.isdigit():
        raise StandardContractCutoverError(
            f"{CUTOVER_ENV}={raw!r} is not a whole ticket number; the standard-form "
            "cutover is not applied until it is fixed."
        )
    return int(raw)


def ticket_number(ticket_id: str) -> int | None:
    match = _TICKET_NUMBER_RE.match(ticket_id)
    return int(match.group(1)) if match else None


def repository_name(repository: str) -> str:
    """The bare, lowercase repository name of ``name`` or ``owner/name``."""
    return repository.strip().rsplit("/", 1)[-1].strip().lower()


def _refuse(code: str, description: str, detail: str) -> StandardContractJudgement:
    message = f"{code}: {' '.join(detail.split())}"
    if len(message) > _MAX_DETAIL:
        message = message[: _MAX_DETAIL - 3] + "..."
    return StandardContractJudgement(
        refusal=ModelEvidenceCheckResult(
            evidence_id="contract",
            description=description,
            status=EnumEvidenceCheckStatus.FAILED,
            message=message,
        )
    )


def refuse_schema_invalid(description: str, detail: str) -> StandardContractJudgement:
    """A ``contract_schema_invalid`` refusal for a contract that could not be read."""
    return _refuse(REFUSAL_SCHEMA_INVALID, description, detail)


def judge_standard_contract(
    ticket_id: str,
    full_contract: Mapping[str, Any],
    *,
    checkout_repository: str | None,
    environ: Mapping[str, str] | None = None,
) -> StandardContractJudgement:
    """Judge the complete head contract; the first rule it breaks is the refusal.

    ``checkout_repository`` is the repository (``owner/name`` or ``name``) the
    contract is checked in; None when it cannot be resolved, which leaves the
    second-repository rule unjudged rather than guessed.
    """
    description = f"Standard ticket contract for {ticket_id}"
    try:
        cutover = read_cutover(environ)
    except StandardContractCutoverError as exc:
        return _refuse(REFUSAL_CUTOVER_INVALID, description, str(exc))

    try:
        contract = ModelTicketContract.model_validate(dict(full_contract))
    except ValidationError as exc:
        reasons = "; ".join(
            f"{'.'.join(str(part) for part in err['loc']) or 'contract'}: {err['msg']}"
            for err in exc.errors()
        )
        return refuse_schema_invalid(
            description,
            f"omnibase_core's ModelTicketContract refuses the contract: {reasons}",
        )
    if contract.ticket_id != ticket_id:
        return refuse_schema_invalid(
            description,
            f"the full contract is for {contract.ticket_id!r}, not {ticket_id!r}",
        )

    number = ticket_number(ticket_id)
    at_or_above_cutover = (
        cutover is not None and number is not None and number >= cutover
    )
    major, minor, patch = (int(part) for part in contract.schema_version.split("."))
    is_standard = (major, minor, patch) >= _STANDARD_SCHEMA

    if not is_standard:
        if at_or_above_cutover:
            return _refuse(
                REFUSAL_NOT_STANDARD,
                description,
                f"{ticket_id} is at or above the standard-form cutover "
                f"({CUTOVER_ENV}={cutover}) and its contract is schema "
                f"{contract.schema_version}; a contract at schema 1.1.0 or later is "
                "required.",
            )
        return StandardContractJudgement(standing=STANDING_GRANDFATHERED)

    stored = contract.contract_fingerprint
    recomputed = contract.compute_fingerprint()
    if stored != recomputed:
        return _refuse(
            REFUSAL_NOT_SYNCED,
            description,
            f"the contract was changed after its last sync: stored fingerprint "
            f"{stored or 'none'}, recomputed fingerprint {recomputed}. Change the "
            "contract through the ticket node so its fingerprint is rewritten.",
        )

    if at_or_above_cutover and checkout_repository:
        named = repository_name(contract.repository or "")
        here = repository_name(checkout_repository)
        if named != here:
            return _refuse(
                REFUSAL_SECOND_REPOSITORY,
                description,
                f"{ticket_id} is at or above the standard-form cutover and its "
                f"contract names repository {contract.repository!r}, but it is "
                f"checked in {checkout_repository!r}; one ticket holds one "
                "repository's work.",
            )
    return StandardContractJudgement()
