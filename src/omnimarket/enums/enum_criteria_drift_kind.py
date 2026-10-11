# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""How a ticket's live acceptance criteria differ from what its contract was accepted against."""

from __future__ import annotations

from enum import StrEnum, unique


@unique
class EnumCriteriaDriftKind(StrEnum):
    """One way the live ticket and the contract's accepted criteria disagree."""

    #: A pinned binding's criterion hash is not the hash of the live text, and no
    #: independent acceptance pins the live text.
    EDITED = "edited"
    #: The contract claims a criterion label the live ticket no longer carries.
    DELETED = "deleted"
    #: The live ticket carries a labelled criterion the contract never recorded.
    ADDED = "added"
    #: Two live criteria resolve to one label with different text, and no
    #: accepted pin says which one was meant.
    DUPLICATE_LABEL = "duplicate_label"


__all__: list[str] = ["EnumCriteriaDriftKind"]
