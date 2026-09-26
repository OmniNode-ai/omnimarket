# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ticket a delegation worked, as one typed identifier (OMN-19514).

A delegation run is joined to the ticket it worked, and through that ticket to
the definition-of-done verdict that judged it, only when both sides spell the
ticket the same way. This module is the one spelling.

The request carries the ticket in ``metadata`` under
:data:`DELEGATION_TICKET_METADATA_KEY`, because every released request consumer
already accepts that map; a declared request field would be refused by the
deployed consumer until a release carried it. The terminal carries it as
``ticket_id`` once a consumer that decodes the key is released.

A value that is not a ticket identifier is never guessed into one. It is
refused by name, and the projection then stores no ticket rather than a wrong
one.
"""

from __future__ import annotations

import re

from pydantic import JsonValue

#: The request ``metadata`` key a caller sets to name the ticket it is working.
DELEGATION_TICKET_METADATA_KEY = "ticket_id"

#: A Linear issue identifier: a team key, a hyphen, and a positive number.
#: The same shape ``ModelPipelineStartCommand`` accepts.
TICKET_ID_PATTERN = re.compile(r"^[A-Z][A-Z0-9]+-[1-9][0-9]*$")


def ticket_id_refusal(value: JsonValue) -> str | None:
    """Return why ``value`` is not a ticket identifier, or None when it is one."""
    if not isinstance(value, str):
        return f"ticket_id is not a string: {type(value).__name__}"
    if not TICKET_ID_PATTERN.fullmatch(value):
        return f"ticket_id {value!r} does not match {TICKET_ID_PATTERN.pattern}"
    return None


__all__ = [
    "DELEGATION_TICKET_METADATA_KEY",
    "TICKET_ID_PATTERN",
    "ticket_id_refusal",
]
