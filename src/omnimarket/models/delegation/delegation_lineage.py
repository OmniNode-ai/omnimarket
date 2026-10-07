# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The delegation a fallback or escalation follows, as one typed value (OMN-20606).

A caller that retries failed work (the merge-drain rung chain: the deployed dev
lane, then an in-process run, then GLM) issues a NEW delegation with its own
correlation id. Before this module the answering ``delegation_events`` row
named nothing about the failure it answered, so the delegation watch view
showed an unrelated success after a failure. Measured on the h201 dev lane on
2026-10-05: 93 of 94 failed delegations that carried a session were answered
by a later delegation within 120 s, and none of those rows named its parent.

The caller states the relation on the request, in ``metadata`` (every released
request consumer already accepts that map), under the three keys below. The
same three keys carry it on the delegate-skill terminal and name the three
``delegation_events`` columns (migration 0055):

* ``parent_correlation_id``: the correlation id of the delegation this one
  follows, a canonical UUID string;
* ``lineage_kind``: ``fallback`` or ``escalation``
  (:class:`~omnimarket.enums.enum_delegation_lineage_kind.EnumDelegationLineageKind`);
* ``parent_failure_cause``: optional, a short token naming why the parent did
  not answer (its terminal failure cause when it had one, else the caller's
  classification, such as ``probe_refused`` or ``exit_124``).

Lineage is attribution, so it never fails a delegation or dead-letters its row.
A malformed value is refused by name and the row records no lineage at all,
rather than half of one or a guessed one.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.enums.enum_delegation_lineage_kind import EnumDelegationLineageKind

#: The request metadata keys, terminal keys and delegation_events columns.
PARENT_CORRELATION_ID_KEY = "parent_correlation_id"
LINEAGE_KIND_KEY = "lineage_kind"
PARENT_FAILURE_CAUSE_KEY = "parent_failure_cause"
LINEAGE_KEYS: frozenset[str] = frozenset(
    {PARENT_CORRELATION_ID_KEY, LINEAGE_KIND_KEY, PARENT_FAILURE_CAUSE_KEY}
)

#: A failure cause token: lowercase, starts with a letter or digit, then
#: letters, digits, ``_``, ``.``, ``:`` or ``-``, at most 64 characters. It
#: admits every ``EnumDelegationTerminalFailureCause`` value.
PARENT_FAILURE_CAUSE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,63}$")


class ModelDelegationLineage(BaseModel):
    """A delegation's parent, the kind of relation, and why the parent failed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    parent_correlation_id: UUID = Field(...)
    lineage_kind: EnumDelegationLineageKind = Field(...)
    parent_failure_cause: str | None = Field(
        default=None, pattern=PARENT_FAILURE_CAUSE_PATTERN.pattern
    )

    def as_columns(self) -> dict[str, str]:
        """The three keys as text: the metadata, terminal and column spelling."""
        columns = {
            PARENT_CORRELATION_ID_KEY: str(self.parent_correlation_id),
            LINEAGE_KIND_KEY: self.lineage_kind.value,
        }
        if self.parent_failure_cause is not None:
            columns[PARENT_FAILURE_CAUSE_KEY] = self.parent_failure_cause
        return columns


def resolve_lineage(
    values: Mapping[str, object],
    *,
    own_correlation_id: UUID | str | None = None,
) -> tuple[ModelDelegationLineage | None, str | None]:
    """Read lineage from ``values`` (request metadata or a terminal).

    Returns ``(lineage, None)`` for a whole, well-formed lineage,
    ``(None, None)`` when no lineage key is present, and ``(None, refusal)``
    otherwise. Never a partial lineage: a kind with no parent, a parent with
    no kind, or a cause with neither is refused by name.
    """
    parent = values.get(PARENT_CORRELATION_ID_KEY)
    kind = values.get(LINEAGE_KIND_KEY)
    cause = values.get(PARENT_FAILURE_CAUSE_KEY)
    if parent is None and kind is None and cause is None:
        return None, None
    if parent is None or kind is None:
        return None, (
            "lineage names a parent_correlation_id and a lineage_kind together; "
            f"got parent_correlation_id={parent!r} lineage_kind={kind!r}"
        )
    if not isinstance(parent, str | UUID):
        return None, f"parent_correlation_id is not a string: {type(parent).__name__}"
    try:
        parent_id = parent if isinstance(parent, UUID) else UUID(parent.strip())
    except ValueError:
        return None, f"parent_correlation_id {parent!r} is not a UUID"
    if own_correlation_id is not None and str(parent_id) == str(own_correlation_id):
        return None, f"a delegation cannot follow itself: {parent_id}"
    if not isinstance(kind, str) or kind not in {
        k.value for k in EnumDelegationLineageKind
    }:
        allowed = ", ".join(k.value for k in EnumDelegationLineageKind)
        return None, f"lineage_kind {kind!r} is not one of {allowed}"
    if cause is not None and (
        not isinstance(cause, str) or not PARENT_FAILURE_CAUSE_PATTERN.fullmatch(cause)
    ):
        return None, (
            f"parent_failure_cause {cause!r} does not match "
            f"{PARENT_FAILURE_CAUSE_PATTERN.pattern}"
        )
    return (
        ModelDelegationLineage(
            parent_correlation_id=parent_id,
            lineage_kind=EnumDelegationLineageKind(kind),
            parent_failure_cause=cause,
        ),
        None,
    )


__all__ = [
    "LINEAGE_KEYS",
    "LINEAGE_KIND_KEY",
    "PARENT_CORRELATION_ID_KEY",
    "PARENT_FAILURE_CAUSE_KEY",
    "PARENT_FAILURE_CAUSE_PATTERN",
    "ModelDelegationLineage",
    "resolve_lineage",
]
