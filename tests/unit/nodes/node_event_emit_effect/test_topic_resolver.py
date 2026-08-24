# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for ``spool/topic_resolver.py`` (OMN-16021).

``resolve_event_type`` must distinguish two cases that both currently raise
``UnknownEventTypeError``:

- ``event_type`` has NO registration at all in ``topics.yaml`` -- must keep
  raising (no silent default topic).
- ``event_type`` IS registered, but its registry entry deliberately declares
  ``fan_out: []`` (a documented "side-channel only, no Kafka fan-out"
  registration, e.g. ``skill.friction_recorded``) -- must NOT raise; callers
  need a non-exception zero-topics result so a legitimately-registered
  zero-fan-out event degrades gracefully everywhere ``resolve_event_type`` is
  called, not just in one handler.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_event_emit_effect.spool.topic_resolver import (
    UnknownEventTypeError,
    resolve_event_type,
)

pytestmark = pytest.mark.unit

# Registered in topics.yaml with `fan_out: []` -- "Side-channel only, no
# Kafka fan-out" by design. Do not change that registration; these tests
# assert on the resolver's behavior against it, not on the registry content.
_ZERO_FAN_OUT_EVENT_TYPE = "skill.friction_recorded"


def test_registered_event_with_empty_fan_out_does_not_raise() -> None:
    """A legitimately-registered zero-fan-out event resolves to no topics.

    Previously this raised ``UnknownEventTypeError``, indistinguishable from
    an event_type with no registration at all -- the exact bug OMN-16021
    reports.
    """
    resolved = resolve_event_type(_ZERO_FAN_OUT_EVENT_TYPE)

    assert resolved == ()


def test_unregistered_event_type_still_raises() -> None:
    """An event_type with NO registration at all must still fail fast."""
    with pytest.raises(UnknownEventTypeError):
        resolve_event_type("totally.unregistered.event.omn16021")
