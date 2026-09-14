# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read a ticket's description as it stood at the moment it was created.

OMN-18332, plan row 5b. The transcriber can only grant automatic acceptance to
a declaration that existed before anything was executed against it, so it needs
the CREATION revision's text -- not the current description, and not a
reconstruction.

The surface is ``documentContentHistory``, reached in two hops:

1. ``issue(id:).documentContent.id`` -- the rich-text document behind the
   description;
2. root ``documentContentHistory(id:)`` -- every revision of that document,
   each carrying ``contentDataSnapshotAt``, ``actorIds`` and ``contentData``.

The creation revision is the entry whose ``contentDataSnapshotAt`` equals the
issue's ``createdAt``.

**``IssueHistory`` is the wrong surface and is deliberately not used.** It
carries no per-revision description text, and it UNDERCOUNTS revisions: a
sampled ticket with two ``documentContentHistory`` entries returned zero
``IssueHistory`` entries with ``updatedDescription`` set. A mechanism built on
it would silently believe an edited ticket was never edited, which is the exact
failure this step exists to prevent.

Verified live 2026-09-13 across eight tickets spanning seven months -- four
never edited, four edited one to three times. In all eight the oldest entry's
``contentDataSnapshotAt`` equalled ``issue.createdAt`` to the millisecond, and a
never-edited ticket returned exactly one entry rather than zero.

Everything here fails CLOSED. An unreadable history, a scope the caller's
application lacks, a transport error, a ticket with no matching entry: all
return ``None``, and ``None`` means the transcriber accepts nothing
automatically for that ticket. Absence is never read as agreement with the
current text.

**This module holds the two queries and the decoding, and performs no I/O.**
The round trips themselves belong to the read-EFFECT (``node_occ_state_effect``),
which is the layer that is allowed to make them. Splitting it that way is not
bookkeeping: it is what lets every fail-closed edge above be exercised without a
network, and it keeps a module that would otherwise mix pure decoding with live
HTTP on the pure side of the imperative-contract boundary.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

__all__ = [
    "HISTORY_QUERY",
    "ISSUE_QUERY",
    "LINEAR_API_KEY_ENV",
    "ModelCreationRevision",
    "ModelTicketDeclaration",
    "build_ticket_declaration",
    "select_creation_revision",
]

logger = logging.getLogger(__name__)

#: The name of the environment variable the key is read from. The VALUE is never
#: logged, never rendered into a contract, and never returned by anything here.
LINEAR_API_KEY_ENV: Final[str] = "LINEAR_API_KEY"

#: How close two timestamps must be to name the same revision. Linear returns
#: both fields at millisecond resolution and the eight-ticket probe matched
#: exactly, so this is a guard against serialisation rounding rather than a
#: tolerance window -- wide enough that a re-serialised millisecond still
#: matches, far too narrow to admit a real later edit.
_SNAPSHOT_MATCH_TOLERANCE_S: Final[float] = 0.002

ISSUE_QUERY: Final[str] = """
query($id: String!) {
  issue(id: $id) {
    id
    identifier
    createdAt
    description
    creator { id }
    documentContent { id }
  }
}
"""

HISTORY_QUERY: Final[str] = """
query($id: String!) {
  documentContentHistory(id: $id) {
    history {
      id
      contentDataSnapshotAt
      actorIds
      contentData
    }
  }
}
"""


@dataclass(frozen=True, slots=True)
class ModelCreationRevision:
    """The description document as it stood when the ticket was created."""

    #: The revision's snapshot time, equal to the issue's ``createdAt``.
    snapshot_at: datetime
    #: The rich-text document. Projected by
    #: :func:`omnimarket.occ_criterion_normalizer.rich_text_comparison_text`.
    content_data: dict[str, Any]
    #: The actor who wrote it -- the acceptor of every criterion it declares.
    #: ``None`` when the revision records no actor, which makes the transcriber
    #: fall back to the issue's creator rather than accept anonymously.
    actor_id: str | None


@dataclass(frozen=True, slots=True)
class ModelTicketDeclaration:
    """Everything the transcriber needs about one ticket, in one read."""

    identifier: str
    created_at: datetime
    description: str
    creator_id: str | None
    #: ``None`` when the creation revision could not be resolved. The caller
    #: must treat that as "accept nothing automatically", never as "unchanged".
    creation_revision: ModelCreationRevision | None


def _parse_timestamp(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def select_creation_revision(
    history: list[dict[str, Any]] | None, created_at: datetime
) -> ModelCreationRevision | None:
    """The entry whose snapshot time is the ticket's creation time.

    Pure, so the selection rule is testable without a transport. Returns
    ``None`` for an empty history, an unparseable entry, or a history whose
    oldest entry post-dates creation -- each of which means the creation
    declaration is not recoverable, which is not the same as unchanged.
    """
    if not history:
        return None
    for entry in history:
        if not isinstance(entry, dict):
            continue
        snapshot_at = _parse_timestamp(entry.get("contentDataSnapshotAt"))
        if snapshot_at is None:
            continue
        if (
            abs((snapshot_at - created_at).total_seconds())
            > _SNAPSHOT_MATCH_TOLERANCE_S
        ):
            continue
        content_data = entry.get("contentData")
        if not isinstance(content_data, dict):
            # A matching entry whose document is missing is worse than no
            # match: it would otherwise read as a ticket that declared nothing.
            return None
        actor_ids = entry.get("actorIds")
        actor_id: str | None = None
        if isinstance(actor_ids, list):
            for candidate in actor_ids:
                if isinstance(candidate, str) and candidate.strip():
                    actor_id = candidate.strip()
                    break
        return ModelCreationRevision(
            snapshot_at=snapshot_at, content_data=content_data, actor_id=actor_id
        )
    return None


def build_ticket_declaration(
    identifier: str,
    issue: Any,
    history: Any,
) -> ModelTicketDeclaration | None:
    """Assemble one ticket's declaration from payloads someone else fetched.

    Pure by construction: it performs no I/O and holds no transport. The two
    GraphQL round trips belong to the read-EFFECT, which is the only layer in
    this repo allowed to make them; keeping the decoding here is what lets every
    fail-closed edge be tested without a network and keeps this module on the
    pure side of the imperative-contract boundary.

    Returns ``None`` only when the issue itself is unusable. A readable issue
    whose history cannot be resolved returns a declaration with
    ``creation_revision`` set to ``None`` -- the transcriber then emits drafts,
    which is a different and more useful outcome than emitting nothing.
    """
    if not isinstance(issue, dict):
        logger.warning(
            "could not read issue %s; no bindings will be emitted", identifier
        )
        return None

    created_at = _parse_timestamp(issue.get("createdAt"))
    if created_at is None:
        logger.warning(
            "issue %s has no readable createdAt; no acceptance can be timestamped",
            identifier,
        )
        return None

    creator = issue.get("creator")
    creator_id = (
        str(creator.get("id"))
        if isinstance(creator, dict) and creator.get("id")
        else None
    )

    creation_revision = select_creation_revision(
        history if isinstance(history, list) else None, created_at
    )
    if creation_revision is None:
        logger.warning(
            "no creation revision resolved for %s; every binding will be a draft",
            identifier,
        )

    description = issue.get("description")
    return ModelTicketDeclaration(
        identifier=str(issue.get("identifier") or identifier),
        created_at=created_at,
        description=description if isinstance(description, str) else "",
        creator_id=creator_id,
        creation_revision=creation_revision,
    )
