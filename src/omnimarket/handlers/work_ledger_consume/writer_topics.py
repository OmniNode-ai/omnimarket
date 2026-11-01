# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Take every ledger writer's command topic, skipping one the broker cannot serve (OMN-21288).

On 2026-10-31 the ledger host h200 restarted its serve in a loop from 01:50Z: its writer list named
dev-jake-macbook-pro-2, whose command topic the dev broker did not hold, so the bus client timed out
starting that consumer, the error left ``WorkLedgerAppendHost.start`` and the process exited. One
writer the broker cannot serve stopped the ledger for every writer.

A writer whose topic cannot be subscribed cannot write anyway, so its subscribe failure is logged
and the topic is returned as unserved; the other writers keep appending. The next resubscribe (the
consume watch, a writer-set change) takes it again. Only writer topics are taken here: the shared
command topic and the mirror topics stay fatal in the host.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterable
from functools import partial
from typing import Any, Protocol

logger = logging.getLogger(__name__)


class ProtocolWriterTopics(Protocol):
    def writer_command_topic(self, principal: str) -> str: ...


async def take_writer_topics(
    take: Callable[[str, Callable[[Any], Awaitable[None]], str], Awaitable[None]],
    group: str,
    principals: Iterable[str],
    topics: ProtocolWriterTopics,
    enqueue: Callable[..., Awaitable[None]],
) -> tuple[str, ...]:
    """Subscribe each writer's topic in name order; return the topics whose subscribe failed."""
    unserved: list[str] = []
    for principal in sorted(principals):
        topic = topics.writer_command_topic(principal)
        try:
            await take(topic, partial(enqueue, writer_principal=principal), group)
        except Exception as exc:  # an absent topic cannot carry a row; serve the others
            unserved.append(topic)
            logger.error(
                "work-ledger host: writer topic %s is not served (%s: %s); "
                "retaken on the next resubscribe",
                topic,
                type(exc).__name__,
                exc,
            )
    return tuple(unserved)
