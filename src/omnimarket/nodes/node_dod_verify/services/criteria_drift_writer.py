# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Name who wrote a criteria drift: a ticket edit or the contract.

The drift check compares a contract's pinned criteria with the live ticket, and says only that
they differ. It worded an ADDED or DELETED claim as an edit of the ticket even where the ticket
had never been edited and the contract claimed a slice, or its own wording, of the criteria. The
writer is measured from two reads, never inferred from the kind of drift:

* the ticket's history entries that rewrote its description (actor and time), and
* the time the contract version at the pull request head was written.

A description edit after the contract was written means the ticket moved under the contract:
``writer=ticket-edit`` with the first such edit's actor and time. A readable history with no
such edit means the contract disagrees with a ticket that did not move: ``writer=contract``.
Anything unread (the history, the contract's write time) is ``writer=unmeasured`` with the
reason, never a guess in either direction.
"""

from __future__ import annotations

from datetime import datetime

from omnimarket.enums.enum_criteria_drift_writer import EnumCriteriaDriftWriter
from omnimarket.models.model_criteria_drift_writer import (
    ModelCriteriaDriftWriter,
    ModelDescriptionEditsRead,
)

__all__ = ["attribute_drift_writer"]


def attribute_drift_writer(
    *,
    contract_path: str,
    head_sha: str,
    written_at: datetime | None,
    history: ModelDescriptionEditsRead,
) -> ModelCriteriaDriftWriter:
    """The writer of a drift on one ticket.

    ``written_at`` is when the contract version at ``head_sha`` was committed, None when that
    could not be read.
    """
    if history.unavailable_reason is not None:
        return _unmeasured(
            f"the ticket history was not read: {history.unavailable_reason}"
        )
    if written_at is None:
        return _unmeasured(
            f"the write time of {contract_path} at the head was not read"
        )
    later = sorted(
        (edit for edit in history.edits if edit.created_at > written_at),
        key=lambda edit: edit.created_at,
    )
    if later:
        first = later[0]
        return ModelCriteriaDriftWriter(
            writer=EnumCriteriaDriftWriter.TICKET_EDIT,
            actor=first.actor,
            at=first.created_at,
        )
    return ModelCriteriaDriftWriter(
        writer=EnumCriteriaDriftWriter.CONTRACT,
        contract_path=contract_path,
        head_sha=head_sha,
    )


def _unmeasured(reason: str) -> ModelCriteriaDriftWriter:
    return ModelCriteriaDriftWriter(
        writer=EnumCriteriaDriftWriter.UNMEASURED, reason=reason
    )
