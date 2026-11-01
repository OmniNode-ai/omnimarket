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

import asyncio
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
    """Subscribe every writer's topic at once; return, in name order, the topics whose subscribe failed."""

    async def one(principal: str) -> str | None:
        topic = topics.writer_command_topic(principal)
        try:
            await take(topic, partial(enqueue, writer_principal=principal), group)
        except Exception as exc:  # an absent topic cannot carry a row; serve the others
            logger.error(
                "work-ledger host: writer topic %s is not served (%s: %s); "
                "retaken on the next resubscribe",
                topic,
                type(exc).__name__,
                exc,
            )
            return topic
        return None

    # Each writer's group joins on its own, and a join can hold for many seconds on the broker
    # (OMN-20275): started one after another, the last writer in name order waited for all the others.
    joined = await asyncio.gather(*(one(p) for p in sorted(principals)))
    return tuple(topic for topic in joined if topic is not None)


def subscriber(
    bus: object,
    unsubscribes: list[Callable[[], Awaitable[None]]],
    subscribe: Callable[..., Awaitable[Callable[[], Awaitable[None]]]],
) -> Callable[[str, Callable[[Any], Awaitable[None]], str], Awaitable[None]]:
    """A ``take`` that subscribes from the earliest committed position and keeps the unsubscribe."""

    async def take(
        topic: str, callback: Callable[[Any], Awaitable[None]], group: str
    ) -> None:
        unsubscribes.append(await subscribe(bus, topic, callback, group, "earliest"))

    return take


async def take_together(
    jobs: Iterable[Awaitable[object]], release: Callable[[], Awaitable[None]]
) -> None:
    """Run every subscribe job at once; when one fails, release what the others took and re-raise it."""
    results = await asyncio.gather(*jobs, return_exceptions=True)
    failed = [result for result in results if isinstance(result, BaseException)]
    if failed:
        await release()
        raise failed[0]
