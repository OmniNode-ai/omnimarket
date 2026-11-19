# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The panel reducer's pure decision: fold one projected event against the held row.

Module-level pure functions plus :class:`WorkStatePanelRowCommitter`. No I/O and
no clock of its own — the decision's ``now`` is always a parameter, so the fold is
a deterministic ``delta(held, event, now) -> decision`` that a test can pin.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import ValidationError

from omnimarket.models.work_state_panel.enum_panel_decision import (
    EnumPanelDecision,
)
from omnimarket.models.work_state_panel.enum_panel_refusal import EnumPanelRefusal
from omnimarket.models.work_state_panel.model_work_state_panel_body import (
    ModelWorkStatePanelBody,
)
from omnimarket.models.work_state_panel.model_work_state_panel_decision import (
    ModelWorkStatePanelDecision,
)
from omnimarket.models.work_state_panel.model_work_state_panel_entry import (
    ModelWorkStatePanelEntry,
)
from omnimarket.models.work_state_panel.model_work_state_panel_row import (
    ModelWorkStatePanelRow,
)
from omnimarket.models.work_state_projection.enum_work_item_state import (
    EnumWorkItemState,
)
from omnimarket.models.work_state_projection.enum_work_state_source import (
    EnumWorkStateSource,
)
from omnimarket.models.work_state_projection.model_work_state_projected_event import (
    ModelWorkStateProjectedEvent,
)


def body_from_event(event: ModelWorkStateProjectedEvent) -> ModelWorkStatePanelBody:
    """Project one wire event onto the panel body, field by field.

    Never copies the event dict wholesale: each field is transcribed, and the
    counts' string keys and the unread/stale source names are converted to their
    enums. A source name is cut at the first ``:`` and only the part before it is
    kept — the cause text after the colon is dropped and never stored. ``tick_id``
    is not copied. Raises ``ValueError``/``ValidationError`` on any bad key, bad
    source, negative number, or naive timestamp.
    """
    counts = {EnumWorkItemState(key): value for key, value in event.counts.items()}
    unread = tuple(
        EnumWorkStateSource(source.split(":", 1)[0]) for source in event.unread
    )
    stale_sources = tuple(
        EnumWorkStateSource(source.split(":", 1)[0]) for source in event.stale_sources
    )
    return ModelWorkStatePanelBody(
        as_of=event.as_of,
        computed_at=event.computed_at,
        projector=event.projector,
        item_count=event.item_count,
        counts=counts,
        ready_in_scope=event.ready_in_scope,
        unread=unread,
        stale_sources=stale_sources,
    )


def order_key(body: ModelWorkStatePanelBody) -> tuple[datetime, str]:
    """The fold ordering key: the body's instant, then its digest as a tie-breaker."""
    return (body.as_of, body.digest())


def is_newer(
    candidate_key: tuple[datetime, str],
    held_key: tuple[datetime, str],
) -> bool:
    """True when ``candidate_key`` is strictly greater than ``held_key``."""
    return candidate_key > held_key


def publication_id(body: ModelWorkStatePanelBody) -> str:
    """The stable identity of a publication: projector, as_of (microseconds) and digest."""
    as_of = body.as_of.astimezone(UTC).isoformat(timespec="microseconds")
    return f"{body.projector.value}|{as_of}|{body.digest()}"


def refusal_id(event: ModelWorkStateProjectedEvent) -> str:
    """A stable, content-derived id for a refusal: the sha256 of the event's canonical JSON.

    No text of the event is kept anywhere else — only this digest.
    """
    canonical = json.dumps(
        event.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    )
    return "refused-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class WorkStatePanelRowCommitter:
    """The panel's decision on one incoming publication, given the held row and ``now``."""

    def decide_publication(
        self,
        held: ModelWorkStatePanelRow | None,
        event: ModelWorkStateProjectedEvent,
        now: datetime,
        entry: ModelWorkStatePanelEntry,
    ) -> ModelWorkStatePanelDecision:
        """Decide one publication against the held row.

        In order: key mismatch, undecodable, future-dated, then the ordering
        decision (duplicate, superseded, or accepted).
        """
        if event.projector != entry.projector:
            return ModelWorkStatePanelDecision(
                outcome=EnumPanelDecision.KEY_MISMATCH,
                row=None,
            )

        try:
            body = body_from_event(event)
        except (ValueError, ValidationError):
            return ModelWorkStatePanelDecision(
                outcome=EnumPanelDecision.UNDECODABLE,
                row=self._refusal_row(held, EnumPanelRefusal.UNDECODABLE, event, now),
            )

        if body.as_of > now + timedelta(seconds=entry.max_future_skew_s):
            return ModelWorkStatePanelDecision(
                outcome=EnumPanelDecision.FUTURE_DATED,
                row=self._refusal_row(held, EnumPanelRefusal.FUTURE_DATED, event, now),
            )

        pid = publication_id(body)
        if held is not None and held.publication_id == pid:
            return ModelWorkStatePanelDecision(
                outcome=EnumPanelDecision.DUPLICATE,
                row=held,
            )
        if (
            held is not None
            and held.body is not None
            and not is_newer(order_key(body), order_key(held.body))
        ):
            return ModelWorkStatePanelDecision(
                outcome=EnumPanelDecision.SUPERSEDED,
                row=held,
            )

        return ModelWorkStatePanelDecision(
            outcome=EnumPanelDecision.ACCEPTED,
            row=ModelWorkStatePanelRow(
                epoch=(held.epoch if held is not None else 0) + 1,
                as_of=body.as_of,
                publication_id=pid,
                body=body,
                received_at=now,
                last_refusal_code=None,
                last_refusal_at=None,
                last_refusal_id=None,
            ),
        )

    def _refusal_row(
        self,
        held: ModelWorkStatePanelRow | None,
        code: EnumPanelRefusal,
        event: ModelWorkStateProjectedEvent,
        now: datetime,
    ) -> ModelWorkStatePanelRow:
        """The row a refusal writes: only the last-refusal triple changes.

        When ``held`` is None the row is a fresh row at epoch 0 with only the three
        refusal fields set (state EMPTY); when ``held`` is not None it is
        ``held`` with only those three fields updated.
        """
        update: dict[str, Any] = {
            "last_refusal_code": code,
            "last_refusal_at": now,
            "last_refusal_id": refusal_id(event),
        }
        if held is None:
            return ModelWorkStatePanelRow(epoch=0, **update)
        return held.model_copy(update=update)
