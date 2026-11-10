# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
# unimported-handler-allow: HandlerClaudeTokenRotationTick (operation tick) and HandlerClaudeTokenAssignOperation (operation assign) are resolved at boot from handler_routing in contract.yaml, by module path, not by a Python import
"""The rotation tick of a lab host's Claude token pool (OMN-23000), and its envelope-shaped handlers.

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

When the overlay declares a spread rule, a usable login that has been in use longer than the bound
and under which the host's lanes have already started is spread to a token no host's lanes have
used yet, so one account's limit stops benching one host's lanes alone. The assign handler installs
the pool token an operator names on the host's login, with the same journal discipline.

No result, event, line or file this writes carries a token: labels and states only.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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
    ModelClaudeTokenAssignRequest,
    ModelClaudeTokenAssignResult,
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


class HandlerClaudeTokenRotationBase:
    """The pool, login-file and journal steps shared by the rotation tick and the assign handler."""

    def __init__(
        self,
        *,
        pool: HandlerClaudeTokenPool,
        token_file: ProtocolLoginFile,
        host: str,
        events_path: Path,
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._pool = pool
        self._login = token_file
        self._host = host
        self._events_path = events_path
        self._now = now

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


class HandlerClaudeTokenRotation(HandlerClaudeTokenRotationBase):
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
        super().__init__(
            pool=pool,
            token_file=token_file,
            host=host,
            events_path=events_path,
            now=now,
        )
        self._source = marks_source
        self._overlay = overlay

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
            spread = (
                self._spread(request, base, held, entries)
                if held is not None
                and self._overlay.spread is not None
                and self._overlay.spread.enabled
                else None
            )
            if spread is not None:
                return spread
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

    def _spread(
        self,
        request: ModelClaudeTokenRotationRequest,
        base: dict[str, Any],
        held: ModelClaudeTokenPoolEntry,
        entries: tuple[ModelClaudeTokenPoolEntry, ...],
    ) -> ModelClaudeTokenRotationResult | None:
        """Spread a usable login that has been in use longer than the overlay's bound.

        The host's lanes have already started under the login (``uses`` is positive) and it has
        stood on the host longer than the bound, so move it to a token no host's lanes have used
        yet: one account's limit then stops benching this host's lanes alone.
        """
        spread = self._overlay.spread
        since = self._login.since()
        if (
            spread is None
            or not spread.enabled
            or since is None
            or held.uses == 0
            or (self._now() - since).total_seconds()
            <= spread.in_use_longer_than_seconds
        ):
            return None
        candidates = [
            e
            for e in entries
            if e.label != held.label and not e.limited and not e.revoked and e.uses == 0
        ]
        if not candidates:
            return None
        labels = sorted(e.label for e in candidates)
        shift = base["rank"] % len(labels)
        ordered = labels[shift:] + labels[:shift]
        if request.dry_run:
            return ModelClaudeTokenRotationResult(
                **base,
                outcome="planned",
                to_label=ordered[0],
                lines=(
                    f"{request.host}: would spread {base['from_label']} -> {ordered[0]}",
                ),
            )
        for label in ordered:
            secret = self._pool.secret(label)
            if secret is None:
                continue
            event = ModelRotationEvent(
                kind="spread",
                host=request.host,
                from_label=base["from_label"],
                to_label=label,
                reason="spread",
                at=self._now(),
            )
            # The record comes first: a journal that cannot be written stops the spread before the
            # login changes, so a login never changes without its event.
            self._record(event)
            try:
                self._write_login(secret)
            except Exception:
                self._record_failed(event)
                raise
            self._keep_login_managed(label)
            return ModelClaudeTokenRotationResult(
                **base,
                outcome="spread",
                to_label=label,
                events=(event,),
                lines=(
                    f"{request.host}: spread login {base['from_label']} -> {label}",
                ),
            )
        return None

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


class HandlerClaudeTokenAssign(HandlerClaudeTokenRotationBase):
    """Installs the pool token an operator names on the host's login, with the rotation's journal discipline."""

    def __init__(
        self,
        *,
        pool: HandlerClaudeTokenPool,
        token_file: ProtocolLoginFile,
        host: str,
        events_path: Path,
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        super().__init__(
            pool=pool,
            token_file=token_file,
            host=host,
            events_path=events_path,
            now=now,
        )

    def handle(
        self, request: ModelClaudeTokenAssignRequest
    ) -> ModelClaudeTokenAssignResult:
        entries = self._status().tokens
        entry = next((e for e in entries if e.label == request.label), None)
        if entry is None:
            return ModelClaudeTokenAssignResult(
                host=self._host,
                label=request.label,
                outcome="refused",
                refusal="unknown_label",
                lines=(f"{self._host}: {request.label} is not in this host's pool",),
            )
        if entry.revoked:
            return ModelClaudeTokenAssignResult(
                host=self._host,
                label=request.label,
                outcome="refused",
                refusal="revoked",
                lines=(
                    f"{self._host}: {request.label} is revoked; re-add it with --force",
                ),
            )
        if entry.limited:
            return ModelClaudeTokenAssignResult(
                host=self._host,
                label=request.label,
                outcome="refused",
                refusal="limited",
                lines=(
                    f"{self._host}: {request.label} is limited until {entry.until or 'its reset'}",
                ),
            )
        if self._login.fingerprint() == entry.fingerprint:
            return ModelClaudeTokenAssignResult(
                host=self._host,
                label=request.label,
                outcome="unchanged",
                from_label=request.label,
                lines=(f"{self._host}: the login is already {request.label}",),
            )
        from_label = next(
            (e.label for e in entries if e.fingerprint == self._login.fingerprint()),
            None,
        )
        if request.dry_run:
            return ModelClaudeTokenAssignResult(
                host=self._host,
                label=request.label,
                outcome="assigned",
                from_label=from_label,
                lines=(f"{self._host}: would assign {request.label}",),
            )
        secret = self._pool.secret(request.label)
        if secret is None:
            return ModelClaudeTokenAssignResult(
                host=self._host,
                label=request.label,
                outcome="refused",
                refusal="unknown_label",
                lines=(
                    f"{self._host}: {request.label} is in this host's pool but its stored token "
                    "is missing; re-add it with --force",
                ),
            )
        event = ModelRotationEvent(
            kind="rotated",
            host=self._host,
            from_label=from_label,
            to_label=request.label,
            reason="assigned",
            at=self._now(),
        )
        # The record comes first: a journal that cannot be written stops the assign before the login
        # changes, so a login never changes without its event.
        self._record(event)
        try:
            self._write_login(secret)
        except Exception:
            self._record_failed(event)
            raise
        self._keep_login_managed(request.label)
        return ModelClaudeTokenAssignResult(
            host=self._host,
            label=request.label,
            outcome="assigned",
            from_label=from_label,
            lines=(f"{self._host}: assigned {request.label}",),
        )


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


class HandlerClaudeTokenAssignOperation:
    """Envelope-shaped handler for the ``assign`` operation.

    The assign handler takes ports (pool, login file), so this wrapper takes none and assembles
    the production ports per request, through the same path the CLI assign uses.
    """

    def handle(
        self, request: ModelClaudeTokenAssignRequest
    ) -> ModelClaudeTokenAssignResult:
        from omnimarket.nodes.node_claude_token_pool_effect.runtime_claude_token_pool import (
            run_assign,
        )

        return run_assign(request.host, request.label, dry_run=request.dry_run)
