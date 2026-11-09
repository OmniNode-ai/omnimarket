# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
# unimported-handler-allow: HandlerClaudeTokenRotationTick is resolved at boot from handler_routing in contract.yaml (operation tick), by module path, not by a Python import
"""The rotation tick of a lab host's Claude token pool (OMN-23000), and its envelope-shaped handler.

Measured 2026-11-07 (lane token-swap-1335): five hosts ran on one limited account for days because
nothing swapped a login off a limited token, every host's pool drew the same first token, per-host
pools disagreed about which tokens were limited or revoked, and a stale revoked mark stood on two
hosts. One tick, on every declared host, does the four things the pool never did on its own:

1. re-sync the pool's limited and revoked marks from the one source the overlay names, refusing a
   source that cannot be read or is older than its bound, so a dead source never erases a mark;
2. set the host's draw order to the sorted labels rotated by the host's rank in the overlay, so no
   two hosts draw the same first token;
3. read which pool token the host's login is (by fingerprint) and, when it is limited or revoked or
   the host has no login, swap the login to a usable token: the usable tokens rotated by the host's
   rank, so hosts that must move spread across the usable accounts. A usable login is never touched;
   a login the pool does not hold is left alone and named;
4. append one event per rotation, and one when nothing usable is left, to the overlay's events file.

No result, event, line or file this writes carries a token: labels and states only.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from pydantic import SecretStr

from omnimarket.nodes.node_claude_token_pool_effect.handlers.handler_claude_token_pool import (
    HandlerClaudeTokenPool,
)
from omnimarket.nodes.node_claude_token_pool_effect.models.model_claude_token_pool import (
    ModelClaudeTokenPoolEntry,
    ModelClaudeTokenPoolRequest,
    ModelClaudeTokenPoolResult,
)
from omnimarket.nodes.node_claude_token_pool_effect.models.model_claude_token_rotation import (
    ModelClaudeTokenRotationRequest,
    ModelClaudeTokenRotationResult,
    ModelRotationEvent,
    ModelRotationMarksSync,
    ModelRotationOverlay,
)
from omnimarket.nodes.node_claude_token_pool_effect.protocols.protocol_rotation_ports import (
    ProtocolLoginFile,
    ProtocolMarksSource,
)

_MAX_REINSTALLS = 3


def _utc_now() -> datetime:
    return datetime.now(UTC)


class HandlerClaudeTokenRotation:
    """Runs one tick for one host against its pool, its login file and the marks source."""

    def __init__(
        self,
        *,
        pool: HandlerClaudeTokenPool,
        token_file: ProtocolLoginFile,
        marks_source: ProtocolMarksSource,
        overlay: ModelRotationOverlay,
        host: str,
        events_path: Path,
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._pool = pool
        self._login = token_file
        self._source = marks_source
        self._overlay = overlay
        self._host = host
        self._events_path = events_path
        self._now = now

    def handle(
        self, request: ModelClaudeTokenRotationRequest
    ) -> ModelClaudeTokenRotationResult:
        rank = self._overlay.rank(request.host)
        if rank is None:
            raise ValueError(f"{request.host} is not a host of the overlay")
        sync, effective = self._sync_marks(request)
        entries = (effective or self._status(dry_run=request.dry_run)).tokens
        order = self._order(entries, rank)
        if not request.dry_run and order:
            self._pool.handle(
                ModelClaudeTokenPoolRequest(operation="reorder", labels=order)
            )
        by_label = {e.label: e for e in entries}
        fingerprint = self._login.fingerprint()
        held = next((e for e in entries if e.fingerprint == fingerprint), None)
        if fingerprint is None:
            state = "absent"
        elif held is None:
            state = "unmanaged"
        elif held.revoked:
            state = "revoked"
        elif held.limited:
            state = "limited"
        else:
            state = "usable"
        base = {
            "host": request.host,
            "rank": rank,
            "in_use_state": state,
            "from_label": held.label if held else None,
            "order": order,
            "marks_sync": sync,
        }
        if state == "usable":
            return ModelClaudeTokenRotationResult(
                **base,
                outcome="kept",
                lines=(
                    f"{request.host}: login {held.label if held else ''} is usable",
                ),
            )
        if state == "unmanaged":
            return ModelClaudeTokenRotationResult(
                **base,
                outcome="unmanaged",
                lines=(
                    f"{request.host}: the login is not a token of this pool; left alone",
                ),
            )
        reason = "absent" if state == "absent" else state
        usable = [
            by_label[label]
            for label in sorted(by_label)
            if not by_label[label].limited and not by_label[label].revoked
        ]
        usable = [e for e in usable if held is None or e.label != held.label]
        shift = rank % len(usable) if usable else 0
        candidates = usable[shift:] + usable[:shift]
        if request.dry_run:
            to_label = candidates[0].label if candidates else None
            return ModelClaudeTokenRotationResult(
                **base,
                outcome="planned",
                to_label=to_label,
                lines=(
                    f"{request.host}: would swap {base['from_label']} -> {to_label} ({reason})",
                ),
            )
        for chosen in candidates:
            secret = self._pool.secret(chosen.label)
            if secret is None:
                continue
            event = ModelRotationEvent(
                kind="rotated",
                host=request.host,
                from_label=held.label if held else None,
                to_label=chosen.label,
                reason=reason,
                at=self._now(),
            )
            # The record comes first: a journal that cannot be written stops the swap before the login
            # changes, so a login never changes without its event.
            self._record(event)
            try:
                self._write_login(secret)
            except Exception:
                self._record_failed(event)
                raise
            self._keep_login_managed(chosen.label)
            return ModelClaudeTokenRotationResult(
                **base,
                outcome="swapped",
                to_label=chosen.label,
                events=(event,),
                lines=(
                    f"{request.host}: swapped login {base['from_label']} -> {chosen.label} ({reason})",
                ),
            )
        event = ModelRotationEvent(
            kind="no_usable",
            host=request.host,
            from_label=held.label if held else None,
            to_label=None,
            reason=reason,
            at=self._now(),
        )
        self._record(event)
        alarm = (
            f"ALARM claude-pool host={request.host} login {base['from_label']} is {reason} "
            "and no usable token is left in the pool"
        )
        return ModelClaudeTokenRotationResult(
            **base, outcome="no_usable", events=(event,), alarm=alarm, lines=(alarm,)
        )

    # --- steps ------------------------------------------------------------------------------------

    def _status(self, *, dry_run: bool = False) -> ModelClaudeTokenPoolResult:
        return self._pool.handle(
            ModelClaudeTokenPoolRequest(operation="status", dry_run=dry_run)
        )

    def _keep_login_managed(self, label: str) -> None:
        """Re-install ``label`` while the pool holds a different fingerprint than the login just written.

        ``add --force`` can replace the label between the candidate being read and the login being
        written; the old credential would then be installed under a fingerprint the pool no longer
        holds, and every later tick would leave it alone as unmanaged.
        """
        for _ in range(_MAX_REINSTALLS):
            current = next((e for e in self._status().tokens if e.label == label), None)
            if current is None or current.fingerprint == self._login.fingerprint():
                return
            fresh = self._pool.secret(label)
            if fresh is None:
                return
            self._write_login(fresh)

    @staticmethod
    def _order(
        entries: tuple[ModelClaudeTokenPoolEntry, ...], rank: int
    ) -> tuple[str, ...]:
        labels = sorted(e.label for e in entries)
        if not labels:
            return ()
        shift = rank % len(labels)
        return tuple(labels[shift:] + labels[:shift])

    def _sync_marks(
        self, request: ModelClaudeTokenRotationRequest
    ) -> tuple[ModelRotationMarksSync, ModelClaudeTokenPoolResult | None]:
        """Sync the source's marks; the second value is the pool as the sync leaves it, when it ran.

        A dry run applies the same marks to a view of the pool and persists neither the pool nor the mirror,
        so it decides on the marks a real tick decides on.
        """
        if self._overlay.marks_source.host == request.host:
            return ModelRotationMarksSync(state="is_source"), None
        read = self._source.read()
        if read.state != "read" or read.at is None:
            return ModelRotationMarksSync(state="unreadable", detail=read.detail), None
        age = (self._now() - read.at).total_seconds()
        if age > self._overlay.marks_source.max_age_seconds:
            return (
                ModelRotationMarksSync(
                    state="stale",
                    detail=f"source marks are {int(age)} s old, over the {self._overlay.marks_source.max_age_seconds} s bound",
                ),
                None,
            )
        done = self._pool.handle(
            ModelClaudeTokenPoolRequest(
                operation="sync_marks",
                marks=read.marks,
                marks_at=read.at,
                dry_run=request.dry_run,
            )
        )
        return (
            ModelRotationMarksSync(
                state="applied" if done.synced else "unchanged",
                changed=done.synced,
                detail="dry run" if request.dry_run else "",
            ),
            done,
        )

    def _write_login(self, secret: SecretStr) -> None:
        self._login.write(secret)

    def _record_failed(self, event: ModelRotationEvent) -> None:
        """Name the rotation whose login write failed, so the record never claims a swap that did not happen."""
        # the login write's own error is the one to surface
        with contextlib.suppress(OSError):
            self._record(
                event.model_copy(update={"kind": "rotation_failed", "at": self._now()})
            )

    def _record(self, event: ModelRotationEvent) -> None:
        self._events_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._events_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as out:
            out.write(event.model_dump_json() + "\n")


class HandlerClaudeTokenRotationTick:
    """Envelope-shaped handler for the ``tick`` operation.

    The runtime builds a routed handler from its injectable parameters alone. The rotation
    handler takes ports (pool, login file, marks source, overlay), so this wrapper takes none
    and assembles the production ports per request, through the same path the CLI tick uses.
    """

    def handle(
        self, request: ModelClaudeTokenRotationRequest
    ) -> ModelClaudeTokenRotationResult:
        from omnimarket.nodes.node_claude_token_pool_effect.runtime_claude_token_pool import (
            run_tick,
        )

        return run_tick(request.host, dry_run=request.dry_run)
