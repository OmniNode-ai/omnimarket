# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A new optional wire field must not reach a consumer that predates it (OMN-18852).

`published_at` was added to `ModelDelegateSkillRequest` by OMN-18852 so queue
wait -- publish to handler pickup -- is measurable at a definition-B handler,
which receives the typed request and never the Kafka record.

It shipped WITHOUT `exclude_if`, and that broke every wrapper delegation on the
`.201` dev lane between 2026-09-19T22:10:50Z and the fix these tests pin.

The mechanism, which is a ROLLOUT property and not a modelling mistake:

* the producer advances in SECONDS -- the dispatch venv reconciled to the
  producing commit 16 s after it merged;
* the consumer advances in MINUTES TO HOURS -- the lane's runtime was baked
  into a 21:06Z image;
* the model declares ``extra="forbid"``;
* so the producer emitted ``"published_at": null`` to a consumer with no such
  field, and every record was refused as publisher-malformed 6 s in
  (``published_at: Extra inputs are not permitted``, offset 725).

Both sides reported version ``0.4.134``. A version comparison could not detect
this and did not, which is why the guard has to be a serialisation property
rather than a release-ordering convention someone remembers.

The rule these tests enforce: **a new optional field on a wire contract whose
consumers forbid extras either lands consumer-first, or is excluded when
unset.** The second is what this model does.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from omnimarket.models.delegation.wire.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)

# The field set a consumer built BEFORE OMN-18852 accepts. `published_at` is
# deliberately absent: that absence is the whole point of this module. This is
# pinned as a literal rather than derived from the model, because deriving it
# from the model under test would make it agree with any change the model makes
# -- including the one that caused the outage.
_PRE_OMN18852_ACCEPTED_FIELDS = frozenset(
    ModelDelegateSkillRequest.model_fields.keys()
) - {"published_at"}


def _minimal_request(**overrides: object) -> ModelDelegateSkillRequest:
    """Build a request the way a caller with no queue instrumentation does."""
    payload: dict[str, object] = {
        "prompt": "summarise a kafka consumer group in two sentences",
        "task_type": "document",
        "source": "claude-code",
    }
    payload.update(overrides)
    return ModelDelegateSkillRequest(**payload)  # type: ignore[arg-type]


@pytest.mark.unit
def test_default_request_does_not_serialise_published_at() -> None:
    """The regression itself: an unstamped request must not emit the key.

    RED before the fix -- the dump carried ``"published_at": null``.
    """
    wire = json.loads(_minimal_request().model_dump_json())

    assert "published_at" not in wire, (
        "OMN-18852: an unstamped request serialised `published_at`, so a "
        "consumer predating this field refuses the record as "
        "publisher-malformed. This is the 2026-09-19T22:10:50Z dev-lane "
        f"outage. Emitted keys: {sorted(wire)}"
    )


@pytest.mark.unit
def test_default_request_is_accepted_by_a_pre_omn18852_consumer() -> None:
    """Consumer-first compatibility, stated as a property rather than an order.

    Every key a default-built request puts on the wire must be one a consumer
    built before this field existed already accepts. Anything else is a record
    that a not-yet-rebuilt consumer with ``extra="forbid"`` will refuse.
    """
    emitted = set(json.loads(_minimal_request().model_dump_json()))

    unknown_to_old_consumer = emitted - _PRE_OMN18852_ACCEPTED_FIELDS
    assert not unknown_to_old_consumer, (
        "OMN-18852: a default-built request emits "
        f"{sorted(unknown_to_old_consumer)}, which a consumer predating "
        "OMN-18852 declares no field for and forbids as an extra. A new "
        "optional field on this contract lands consumer-first or is excluded "
        "when unset."
    )


@pytest.mark.unit
def test_a_stamped_request_still_serialises_published_at() -> None:
    """Excluding when unset must not make the field unusable when it IS set.

    The positive control. Without it, deleting the field entirely would pass
    the two tests above, and queue wait would silently never be measurable.
    """
    stamped = datetime(2026, 9, 19, 20, 8, 15, tzinfo=UTC)

    wire = json.loads(_minimal_request(published_at=stamped).model_dump_json())

    assert "published_at" in wire, (
        "OMN-18852: a STAMPED request dropped `published_at`, so queue wait "
        "can never be measured and the exclusion has over-reached."
    )
    assert ModelDelegateSkillRequest(**wire).published_at == stamped


@pytest.mark.unit
def test_round_trip_through_the_wire_preserves_the_unstamped_case() -> None:
    """A consumer carrying THIS model still reads an unstamped request."""
    wire = json.loads(_minimal_request().model_dump_json())

    assert ModelDelegateSkillRequest(**wire).published_at is None, (
        "OMN-18852: an omitted `published_at` must read back as None -- not "
        "measured -- never as a zero queue wait."
    )


@pytest.mark.unit
def test_a_naive_published_at_is_still_refused() -> None:
    """The exclusion must not weaken the timezone refusal it sits beside.

    Producer and consumer are separate processes and need not share a local
    zone, so a naive stamp would silently produce a queue wait wrong by hours.
    """
    with pytest.raises(ValueError, match="timezone-aware"):
        _minimal_request(published_at=datetime(2026, 9, 19, 20, 8, 15))
