# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Canonical injected-key-stripping path for projection ``handle()`` shims.

The runtime auto-wiring (omnibase_infra ``handler_wiring._invoke_projection``)
dispatches projection reducers through ``handle(input_data)`` — not
``project()`` — after injecting bookkeeping keys into the event payload dict:

* ``_db``          -> the sync ``DatabaseAdapter`` the reducer writes through
* ``_event_type``  -> the event_type derived from the source topic
* ``_topic``       -> the source Kafka topic
* ``_envelope_id`` -> the dispatched envelope's stable UUID identity, injected
  only when the envelope carries a coercible ``envelope_id``

This key set is a CROSS-REPO SEAM, and it drifted once (OMN-16249). The
producer side lives in ``omnibase_infra`` and enumerates the keys as separate
assignment statements, so adding one there is a one-line change with no
compile-time link to this module. ``_envelope_id`` was added upstream without
this frozenset being widened; every event carrying it then failed
``extra_forbidden`` here. Because it is injected only ``if envelope_id is not
None``, the breakage was invisible to every reducer fed by internal events that
carry no envelope identity, and surfaced only on the gateway-published path,
where an envelope UUID is always present. ``tests/`` carries a fail-closed
drift guard that harvests the producer's key set from the INSTALLED
``omnibase_infra`` and fails when this frozenset does not cover it — that guard,
not this docstring, is what keeps the two halves matched.

When a projection's inbound event model is declared ``extra="forbid"`` (e.g.
``ModelDepHealthSweepCompletedEvent``), splatting the injected dict straight into
``Model(**input_data)`` raises ``ValidationError`` on injected metadata keys
key. The runtime commits the offset and drops the event, so the consumer group
shows ``LAG=0`` while **no row materializes** — a silent consume-then-drop
(OMN-13825, the 3rd instance of this class after the missing-``handle`` shim and
the unregistered dispatcher).

Rather than hand-roll metadata ``.pop(...)`` calls in every shim
(the pattern that repeatedly drifted), all projection ``handle()`` shims route
their injected-key extraction through :func:`split_projection_input`, so a future
handler that flips its model to ``extra="forbid"`` cannot silently reintroduce
the drop.

Only documented runtime-injected keys are stripped. A blanket
"strip every leading-underscore key" rule would be wrong: domain models may
legitimately declare underscore-aliased fields (e.g.
``ModelSandboxDecisionEvent.runtime_backend`` reads the wire key
``_runtime_backend`` via a Pydantic alias), and those must survive to model
construction.
"""

from __future__ import annotations

from typing import Final

from omnimarket.projection.protocol_database import DatabaseAdapter

__all__: list[str] = [
    "INJECTED_ENVELOPE_ID_KEY",
    "INJECTED_EVENT_TYPE_KEY",
    "INJECTED_TOPIC_KEY",
    "RUNTIME_INJECTED_KEYS",
    "split_projection_input",
]

# The metadata keys the runtime injects into projection ``handle()`` payloads.
_INJECTED_DB_KEY: Final[str] = "_db"
INJECTED_EVENT_TYPE_KEY: Final[str] = "_event_type"
INJECTED_TOPIC_KEY: Final[str] = "_topic"
INJECTED_ENVELOPE_ID_KEY: Final[str] = "_envelope_id"

# Exported so the drift guard asserts against the same object the split uses,
# rather than a second copy that could itself drift.
RUNTIME_INJECTED_KEYS: Final[frozenset[str]] = frozenset(
    {
        _INJECTED_DB_KEY,
        INJECTED_EVENT_TYPE_KEY,
        INJECTED_TOPIC_KEY,
        INJECTED_ENVELOPE_ID_KEY,
    }
)

# The subset handed back to shims as ``injected_meta``. ``_db`` is excluded
# because it is returned separately as the first element of the triple.
_RETURNED_META_KEYS: Final[frozenset[str]] = RUNTIME_INJECTED_KEYS - {_INJECTED_DB_KEY}


def split_projection_input(
    input_data: dict[str, object],
) -> tuple[DatabaseAdapter, dict[str, object], dict[str, object]]:
    """Split a runtime-injected projection ``handle()`` payload into its parts.

    Non-destructive: ``input_data`` is not mutated.

    Args:
        input_data: The dict the runtime hands to ``handle()`` — the event
            payload plus runtime bookkeeping keys.

    Returns:
        A ``(db, model_payload, injected_meta)`` triple where:

        * ``db`` is the ``DatabaseAdapter`` from ``input_data['_db']``.
        * ``model_payload`` is ``input_data`` with runtime metadata removed —
          safe to splat into a strict (``extra="forbid"``) event model. Domain
          fields (including underscore-aliased ones such as
          ``_runtime_backend``) are preserved.
        * ``injected_meta`` carries ``_event_type``/``_topic``/``_envelope_id``
          (each when present) for shims that route on them. ``_envelope_id`` is
          surfaced rather than merely dropped so a reducer can use the stable
          envelope UUID as its durable idempotency key across Kafka
          redeliveries — the reason the runtime injects it at all.

    Raises:
        TypeError: if ``input_data['_db']`` is absent or is not a
            ``DatabaseAdapter``.
    """
    db_raw = input_data.get(_INJECTED_DB_KEY)
    if not isinstance(db_raw, DatabaseAdapter):
        raise TypeError("handle() requires a DatabaseAdapter in input_data['_db']")
    model_payload = {
        key: value
        for key, value in input_data.items()
        if key not in RUNTIME_INJECTED_KEYS
    }
    injected_meta = {
        key: value for key, value in input_data.items() if key in _RETURNED_META_KEYS
    }
    return db_raw, model_payload, injected_meta
