# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Who wrote the disagreement a criteria-drift verdict reports."""

from __future__ import annotations

from enum import StrEnum, unique


@unique
class EnumCriteriaDriftWriter(StrEnum):
    """The party whose write made the contract and the live ticket disagree."""

    #: The ticket's description was edited after the contract was written.
    TICKET_EDIT = "ticket-edit"
    #: The ticket's history shows no description edit after the contract was written,
    #: so the contract claims criteria (or wording) the ticket never carried.
    CONTRACT = "contract"
    #: The history, the contract's write time or the tracker could not be read; no guess.
    UNMEASURED = "unmeasured"


__all__: list[str] = ["EnumCriteriaDriftWriter"]
