# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lane that issued a delegation, as one typed identifier (OMN-19860).

``delegation_events`` recorded which model answered a delegation and how it
ended, but never who asked. Per-lane delegation use could therefore not be
queried from the event stream, and the delegation-health check had to infer it
by joining ledger CLAIM and TERMINAL windows. This module is the one spelling
of the caller's lane, shared by the request, the terminal and the projection.

The request carries the lane in ``metadata`` under
:data:`DELEGATION_CALLER_LANE_METADATA_KEY`, because every released request
consumer already accepts that map. The terminal carries it as ``caller_lane``
once a consumer that decodes the key is released (the OMN-18868 consumer-first
order).

A lane is the same token the rolling work ledger writes in its ``lane=`` cell:
no whitespace and no pipe, so a delegation row and a ledger row name the same
lane in the same vocabulary. A value that is not such a token is refused by
name and never guessed into a lane; the projection then stores no lane rather
than a wrong one.
"""

from __future__ import annotations

import re

from pydantic import JsonValue

#: The request ``metadata`` key a caller sets to name the lane issuing it.
DELEGATION_CALLER_LANE_METADATA_KEY = "caller_lane"

#: A ledger lane token: starts with a letter or digit, then letters, digits,
#: dot, underscore, colon or hyphen, at most 128 characters. It admits every
#: lane slug the ledger carries and the ``session:<id>`` fallback the PR
#: ownership guard resolves, and refuses whitespace, pipes and control bytes.
CALLER_LANE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def caller_lane_refusal(value: JsonValue) -> str | None:
    """Return why ``value`` is not a caller lane, or None when it is one."""
    if not isinstance(value, str):
        return f"caller_lane is not a string: {type(value).__name__}"
    if not CALLER_LANE_PATTERN.fullmatch(value):
        return f"caller_lane {value!r} does not match {CALLER_LANE_PATTERN.pattern}"
    return None


__all__ = [
    "CALLER_LANE_PATTERN",
    "DELEGATION_CALLER_LANE_METADATA_KEY",
    "caller_lane_refusal",
]
