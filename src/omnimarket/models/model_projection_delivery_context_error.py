# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed refusal for a projection writer dispatched with no delivery context."""

from __future__ import annotations


class ProjectionDeliveryContextMissingError(Exception):
    """A projection writer was dispatched without the keys a delivery carries.

    The shared runtime injects ``_partition`` and ``_offset`` only when its consume
    loop could determine the source message's coordinates. A dispatch with no
    delivery context (a runtime tick, a replay, a direct call) arrives without
    them, and a writer must not invent them: a constant coordinate freezes every
    key it stamps. The refusal names the missing keys and the topic, and it carries
    no event content, so refusing one record twice renders the same message.

    This is deliberately not a ``KeyError``, a ``TypeError`` or a validation error:
    the consume boundary reads the event as well formed and the context as the
    runtime's defect, so the offset is withheld rather than the event discarded.
    """

    def __init__(self, missing_keys: tuple[str, ...], topic: str | None) -> None:
        self.missing_keys = missing_keys
        self.topic = topic
        super().__init__(
            "projection writer dispatched with no delivery context: missing "
            f"{', '.join(missing_keys)} (topic={topic if topic else 'unnamed'}); "
            "the coordinates are never defaulted"
        )

    def __reduce__(
        self,
    ) -> tuple[
        type[ProjectionDeliveryContextMissingError], tuple[tuple[str, ...], str | None]
    ]:
        return (type(self), (self.missing_keys, self.topic))


__all__ = ["ProjectionDeliveryContextMissingError"]
