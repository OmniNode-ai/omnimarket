# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Canonical injected-key-stripping path for projection ``handle()`` shims.

The runtime auto-wiring (omnibase_infra ``handler_wiring._invoke_projection``)
dispatches projection reducers through ``handle(input_data)`` — not
``project()`` — after injecting exactly two bookkeeping keys into the event
payload dict (verified against ``handler_wiring.py`` lines 1508-1509):

* ``_db``          -> the sync ``DatabaseAdapter`` the reducer writes through
* ``_event_type``  -> the event_type derived from the source topic

When a projection's inbound event model is declared ``extra="forbid"`` (e.g.
``ModelDepHealthSweepCompletedEvent``), splatting the injected dict straight into
``Model(**input_data)`` raises ``ValidationError`` on the injected ``_event_type``
key. The runtime commits the offset and drops the event, so the consumer group
shows ``LAG=0`` while **no row materializes** — a silent consume-then-drop
(OMN-13825, the 3rd instance of this class after the missing-``handle`` shim and
the unregistered dispatcher).

Rather than hand-roll ``.pop("_db")`` / ``.pop("_event_type")`` in every shim
(the pattern that repeatedly drifted), all projection ``handle()`` shims route
their injected-key extraction through :func:`split_projection_input`, so a future
handler that flips its model to ``extra="forbid"`` cannot silently reintroduce
the drop.

Only the two documented runtime-injected keys are stripped. A blanket
"strip every leading-underscore key" rule would be wrong: domain models may
legitimately declare underscore-aliased fields (e.g.
``ModelSandboxDecisionEvent.runtime_backend`` reads the wire key
``_runtime_backend`` via a Pydantic alias), and those must survive to model
construction.
"""

from __future__ import annotations

from typing import Final

from omnimarket.projection.protocol_database import DatabaseAdapter

__all__: list[str] = ["INJECTED_EVENT_TYPE_KEY", "split_projection_input"]

# The two keys the runtime injects into every projection ``handle()`` payload.
_INJECTED_DB_KEY: Final[str] = "_db"
INJECTED_EVENT_TYPE_KEY: Final[str] = "_event_type"
_RUNTIME_INJECTED_KEYS: Final[frozenset[str]] = frozenset(
    {_INJECTED_DB_KEY, INJECTED_EVENT_TYPE_KEY}
)


def split_projection_input(
    input_data: dict[str, object],
) -> tuple[DatabaseAdapter, dict[str, object], dict[str, object]]:
    """Split a runtime-injected projection ``handle()`` payload into its parts.

    Non-destructive: ``input_data`` is not mutated.

    Args:
        input_data: The dict the runtime hands to ``handle()`` — the event
            payload plus the ``_db`` and ``_event_type`` bookkeeping keys.

    Returns:
        A ``(db, model_payload, injected_meta)`` triple where:

        * ``db`` is the ``DatabaseAdapter`` from ``input_data['_db']``.
        * ``model_payload`` is ``input_data`` with ``_db`` and ``_event_type``
          removed — safe to splat into a strict (``extra="forbid"``) event
          model. Domain fields (including underscore-aliased ones such as
          ``_runtime_backend``) are preserved.
        * ``injected_meta`` carries ``_event_type`` (when present) for shims
          that route on it.

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
        if key not in _RUNTIME_INJECTED_KEYS
    }
    injected_meta = {
        key: value
        for key, value in input_data.items()
        if key == INJECTED_EVENT_TYPE_KEY
    }
    return db_raw, model_payload, injected_meta
