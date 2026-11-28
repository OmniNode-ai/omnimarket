# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Where the alert channel's post policy is read from at run time (OMN-24562).

The policy is a runtime setting. The settings store holds the overlay ``alarm_post_policy``; the sender's own overlay
holds only the bootstrap default, used when the store holds no such overlay or does not answer. No settings file is
read: the one file a sender keeps is the gate's open-episode state, which is state, not a setting.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any

from omnibase_spi.protocols.storage import ProtocolOverlayStore

from omnimarket.handlers.alarm_post_policy import AlarmPostGate, run_ticket_command
from omnimarket.handlers.overlay_store.reader_store import StoreOverlayReader
from omnimarket.handlers.overlay_store.store_wiring import (
    KIND_INPUT,
    build_overlay_store,
)
from omnimarket.models.alarm_post_policy import (
    EnumPostAction,
    ModelAlarmPostPolicy,
    ModelPostDecision,
)

OVERLAY_ID = "alarm_post_policy"
SOURCE_STORE = "store"
SOURCE_BOOTSTRAP = "bootstrap"


def _env_store() -> ProtocolOverlayStore | None:
    """The store the process's bootstrap inputs name; None when they name no kind (a host with no store yet)."""
    if not os.environ.get(KIND_INPUT):
        return None
    return build_overlay_store(os.environ)


def _read_sync(store: ProtocolOverlayStore) -> ModelAlarmPostPolicy:
    async def read() -> ModelAlarmPostPolicy:
        return await StoreOverlayReader(store).read_model(
            OVERLAY_ID, ModelAlarmPostPolicy
        )

    # A fresh loop on its own thread: the caller may already be inside a running loop.
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, read()).result()


class PostPolicySource:
    """The current post policy: the store's when it answers, else the overlay's bootstrap default."""

    def __init__(
        self,
        bootstrap: ModelAlarmPostPolicy | None,
        *,
        store: ProtocolOverlayStore | None = None,
        store_factory: Callable[[], ProtocolOverlayStore | None] = _env_store,
    ) -> None:
        self._bootstrap = bootstrap
        self._store = store
        self._store_factory = store_factory
        self.last_source = ""
        self.last_store_error = ""

    def current(self) -> ModelAlarmPostPolicy | None:
        """None when neither the store nor the bootstrap default gives one: the channel is then ungated."""
        self.last_store_error = ""
        try:
            store = self._store or self._store_factory()
            if store is not None:
                policy = _read_sync(store)
                self.last_source = SOURCE_STORE
                return policy
            self.last_store_error = "no settings store is configured"
        except Exception as exc:
            self.last_store_error = f"{type(exc).__name__}: {exc}"
        self.last_source = SOURCE_BOOTSTRAP if self._bootstrap is not None else ""
        return self._bootstrap


def from_options(
    bootstrap: Mapping[str, object] | ModelAlarmPostPolicy | None,
    *,
    store: ProtocolOverlayStore | None = None,
) -> PostPolicySource:
    policy = (
        ModelAlarmPostPolicy.model_validate(bootstrap)
        if isinstance(bootstrap, Mapping)
        else bootstrap
    )
    return PostPolicySource(policy, store=store)


def decide_with_state_file(
    source: PostPolicySource, state_file: Path, text: str, *, now: datetime
) -> ModelPostDecision | None:
    """The gate's answer for ``text`` with open episodes kept in ``state_file``; None when no policy is available.

    A ticket decision runs the policy's ticket command; when it fails the episode is not marked ticketed, so the next
    run tries again. The caller posts ``decision.text`` for POST, REMINDER, RESOLVED and DETECTOR_DOWN and nothing
    for SUPPRESS and TICKET.
    """
    policy = source.current()
    if policy is None:
        return None
    held: dict[str, Any] = (
        json.loads(state_file.read_text(encoding="utf-8"))
        if state_file.exists()
        else {}
    )
    decision = AlarmPostGate(policy).decide(text, now=now, state=held)
    if decision.action is EnumPostAction.TICKET and not run_ticket_command(
        policy.ticket_command, decision
    ):
        held["open"][decision.key].pop("ticketed", None)
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps(held, sort_keys=True), encoding="utf-8")
    return decision


__all__ = [
    "OVERLAY_ID",
    "SOURCE_BOOTSTRAP",
    "SOURCE_STORE",
    "PostPolicySource",
    "decide_with_state_file",
    "from_options",
]
