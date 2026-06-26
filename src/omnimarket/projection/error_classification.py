# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Canonical projection-error classification (OMN-13634 / WS-F Phase 2).

Two divergent projection error paths previously disagreed on what to do with a
failed event:

* ``handler_wiring`` (omnibase_infra): DLQ-and-commit — dropped the event on ANY
  error, including recoverable infra errors (``UndefinedColumn``,
  ``OperationalError``, connection failures). A migration gap therefore looked
  like a bad event and was silently quarantined as "malformed".
* ``BaseProjectionRunner._handle_message`` (omnimarket): re-raise, no DLQ,
  infinite retry on ALL errors — including genuine poison (a malformed payload
  ``ValidationError`` that will never succeed no matter how often it is retried).

This module is the SINGLE source of truth both paths consult so they apply the
same policy. An error is classified into exactly one of:

* :attr:`ProjectionErrorClass.POISON` — the event itself is bad and will never
  project no matter how many times it is retried (a malformed payload). Policy:
  route to the poison DLQ and COMMIT the offset so it is not retried in a hot
  loop. Captured durably on the DLQ, recoverable by correlation_id.
* :attr:`ProjectionErrorClass.RECOVERABLE` — the event is fine but the
  infrastructure is transiently unable to project it (a missing column from a
  not-yet-applied migration, a server error, a dropped connection). Policy: do
  NOT commit the offset and surface a loud failure; the message is re-read and
  retried until the infra catches up. A migration gap MUST land here, never in
  POISON.

The default for an unrecognised error is RECOVERABLE — the safe direction. A
poison-misclassification drops a real event forever; a recoverable-
misclassification only re-reads a message that may eventually need manual
attention. We never silently drop an error we do not understand.
"""

from __future__ import annotations

from enum import StrEnum

import asyncpg
from asyncpg.exceptions import PostgresError
from pydantic import ValidationError


class ProjectionErrorClass(StrEnum):
    """Classification of a projection-handler failure (OMN-13634)."""

    POISON = "poison"
    RECOVERABLE = "recoverable"


class PoisonEventError(Exception):
    """Raised by a projection handler to declare an event unprocessable.

    A handler that detects a malformed/poison event OUTSIDE pydantic validation
    (e.g. a required field present but semantically invalid) raises this instead
    of dropping the event, so the unified classifier routes it to the poison
    DLQ. Carrying an explicit poison marker keeps the taxonomy declarative
    rather than relying on string-matching error messages.
    """


# Exception types that are always RECOVERABLE: an infra/transport/server signal,
# not a property of the event. ``PostgresError`` is the base of every server-side
# SQL error (``UndefinedColumnError``, the OperationalError-equivalent server
# errors, connection errors) and asyncpg ``InterfaceError`` covers pool/driver
# state. ``OSError`` (and its ``ConnectionError`` / ``TimeoutError`` subclasses)
# covers socket-level broker/DB failures.
_RECOVERABLE_TYPES: tuple[type[BaseException], ...] = (
    PostgresError,
    asyncpg.InterfaceError,
    OSError,
    TimeoutError,
)

# Exception types that are always POISON: the event payload is bad.
_POISON_TYPES: tuple[type[BaseException], ...] = (
    ValidationError,
    PoisonEventError,
)


def classify_projection_error(exc: BaseException) -> ProjectionErrorClass:
    """Classify a projection-handler failure as POISON or RECOVERABLE.

    POISON wins over RECOVERABLE when an exception somehow matches both (it
    cannot today, but the precedence is explicit): a bad payload is bad
    regardless of what surfaced it. Unrecognised errors default to RECOVERABLE
    so an unknown failure is retried, never silently dropped.
    """
    if isinstance(exc, _POISON_TYPES):
        return ProjectionErrorClass.POISON
    if isinstance(exc, _RECOVERABLE_TYPES):
        return ProjectionErrorClass.RECOVERABLE
    return ProjectionErrorClass.RECOVERABLE


__all__ = [
    "PoisonEventError",
    "ProjectionErrorClass",
    "classify_projection_error",
]
