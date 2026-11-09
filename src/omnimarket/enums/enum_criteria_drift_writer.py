# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Who wrote the difference between a ticket's live criteria and its contract (OMN-23184)."""

from __future__ import annotations

from enum import StrEnum, unique


@unique
class EnumCriteriaDriftWriter(StrEnum):
    """The writer a criteria-drift verdict names."""

    #: The ticket's Linear history shows a description edit after the contract's acceptance.
    TICKET_EDIT = "ticket-edit"
    #: The history shows no such edit: the contract file claims a slice or its own wording.
    CONTRACT = "contract"
    #: The history could not be read; nobody is blamed.
    UNMEASURED = "unmeasured"


__all__: list[str] = ["EnumCriteriaDriftWriter"]
