# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One copy of each immutable ledger archive roll per lab host (OMN-23297).

Every lane's snapshot carries all the archive rolls (about 216 MB) and a lane's context
directory kept its own copy: 375 ended lanes held 77 GB of a 926 GB data volume on one lab
host. The host keeps one copy of each roll, in the overlay's store directory; a lane's roll
is made another name of it (a hard link), so removing a lane directory removes only its name.
A roll whose bytes differ from the store's stays the lane's own, and a roll modified within
the settle window is left alone because its unpack may still be writing it.
"""

from __future__ import annotations

import os
from collections.abc import Callable

from ..models.model_lane_ctx_ledger_dedupe import (
    ModelLaneCtxLedgerDedupeCommand,
    ModelLaneCtxLedgerDedupeEvent,
    ModelLaneCtxLedgerDedupeResult,
)
from ..protocols import (
    FileIdentity,
    ProtocolLaneCtxEventPublisher,
    ProtocolLaneCtxFs,
)


class HandlerLaneCtxLedgerDedupe:
    def __init__(
        self,
        *,
        fs: ProtocolLaneCtxFs,
        publisher: ProtocolLaneCtxEventPublisher,
        refused_topic: str,
        completed_topic: str,
        clock: Callable[[], float],
    ) -> None:
        self._fs = fs
        self._publisher = publisher
        self._refused_topic = refused_topic
        self._completed_topic = completed_topic
        self._clock = clock

    def handle(
        self, request: ModelLaneCtxLedgerDedupeCommand
    ) -> ModelLaneCtxLedgerDedupeResult:
        fs = self._fs
        errors: list[str] = []
        kept_own: list[str] = []
        counts = dict.fromkeys(
            (
                "contexts",
                "rolls_seen",
                "stored",
                "already_shared",
                "linked",
                "kept_own",
                "unsettled",
                "bytes_freed",
            ),
            0,
        )
        # In a dry run nothing is stored, so the first roll of each name stands in for the store's copy.
        planned: dict[str, str] = {}

        def refuse(roll: str, reason: str) -> None:
            errors.append(f"{roll}: {reason}")
            self._publisher.publish(
                self._refused_topic,
                ModelLaneCtxLedgerDedupeEvent(roll=roll, reason=reason),
            )

        try:
            contexts = fs.context_dirs(request.runs_root)
            if request.execute:
                fs.ensure_dir(request.store_dir)
        except OSError as exc:
            refuse(request.runs_root, f"unreadable: {type(exc).__name__}")
            contexts = []
        now = self._clock()
        for context in contexts:
            counts["contexts"] += 1
            for roll in fs.rolls(context):
                counts["rolls_seen"] += 1
                mine = fs.identity(roll)
                if mine is None:
                    continue
                if now - mine.mtime < request.settle_seconds:
                    counts["unsettled"] += 1
                    continue
                name = os.path.basename(roll)
                shared = os.path.join(request.store_dir, name)
                reference = shared if fs.identity(shared) else planned.get(name)
                try:
                    if reference is None:
                        if request.execute:
                            fs.link_new(roll, shared)
                        else:
                            planned[name] = roll
                        counts["stored"] += 1
                        continue
                    other = fs.identity(reference)
                    if other is None:
                        continue
                    if (other.dev, other.ino) == (mine.dev, mine.ino):
                        counts["already_shared"] += 1
                    elif other.size == mine.size and fs.same_bytes(reference, roll):
                        if request.execute:
                            fs.replace_with_link(shared, roll)
                        counts["linked"] += 1
                        counts["bytes_freed"] += _freed(mine)
                    else:
                        counts["kept_own"] += 1
                        kept_own.append(roll)
                except OSError as exc:
                    refuse(roll, f"{type(exc).__name__}: {exc.strerror or exc.errno}")
        result = ModelLaneCtxLedgerDedupeResult(
            mode="execute" if request.execute else "dry-run",
            errors=tuple(errors),
            kept_own_rolls=tuple(kept_own),
            **counts,
        )
        self._publisher.publish(self._completed_topic, result)
        return result


def _freed(identity: FileIdentity) -> int:
    """Bytes the volume gets back: a roll still named elsewhere frees nothing."""
    return identity.size if identity.nlink == 1 else 0
