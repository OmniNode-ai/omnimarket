# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The pool status view of a lab host's Claude token pool (OMN-19998).

The pool's own status line names every limited and revoked label, but a host whose copy of the
marks is stale would name them wrongly: the rotation tick syncs the marks from the one source the
overlay names and refuses a source that cannot be read or is over its age bound, and a reader that
skips that sync shows a host revoked that the source long cleared. This handler answers a status
view that names its basis: the host's own pool when the overlay declares it; the source's fresh
marks applied to a view of the pool when the host is off the overlay and the source reads within
its bound; and — when the overlay could not be read on this host, the source cannot be read, or it
is stale — the local copy alone, with no limited or revoked label named from it and the status line
saying so.

Every dependency is injected: the pool, the overlay (``None`` when it could not be read on this
host), the marks source, the local copy's write time and the clock. No result carries a token:
labels, counts and states only.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from omnimarket.nodes.node_claude_token_pool_effect.handlers.handler_claude_token_pool import (
    HandlerClaudeTokenPool,
)
from omnimarket.nodes.node_claude_token_pool_effect.models.model_claude_token_pool import (
    ModelClaudeTokenPoolRequest,
    ModelClaudeTokenPoolResult,
)
from omnimarket.nodes.node_claude_token_pool_effect.models.model_claude_token_pool_status_view import (
    ModelPoolLabelState,
    ModelPoolStatusViewRequest,
    ModelPoolStatusViewResult,
)
from omnimarket.nodes.node_claude_token_pool_effect.models.model_claude_token_rotation import (
    ModelRotationMarksRead,
    ModelRotationOverlay,
)
from omnimarket.nodes.node_claude_token_pool_effect.protocols.protocol_rotation_ports import (
    ProtocolLoginBoard,
    ProtocolMarksSource,
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


class HandlerClaudeTokenPoolStatusView:
    """Answers the pool status of one host, naming which source the answer is built on."""

    def __init__(
        self,
        *,
        pool: HandlerClaudeTokenPool | None = None,
        overlay: ModelRotationOverlay | None = None,
        marks_source: ProtocolMarksSource | None = None,
        local_copy_written_at: Callable[[], datetime | None] = lambda: None,
        now: Callable[[], datetime] = _utc_now,
        login_board: ProtocolLoginBoard | None = None,
    ) -> None:
        # The runtime boot builds this handler with no arguments (the contract binds it to the node): an
        # instance with no overlay answers unsynced, never a guess at a marks source.
        self._pool = pool if pool is not None else HandlerClaudeTokenPool()
        #: ``None`` means the overlay could not be read on this host: no host is the overlay's and
        #: the marks source has no age bound to be judged against, so every answer is unsynced.
        self._overlay = overlay
        self._source = marks_source
        self._copy_written_at = local_copy_written_at
        self._now = now
        #: Where each host's tick says which label its login is; ``None`` places no host on any label.
        self._board = login_board

    def handle(self, request: ModelPoolStatusViewRequest) -> ModelPoolStatusViewResult:
        overlay = self._overlay
        if overlay is not None and self._declares(request.host, overlay):
            return self._from_local_pool(request)
        if overlay is None:
            return self._unsynced(request, "overlay unreadable on this host")
        if self._source is None:
            return self._unsynced(request, "no marks source configured")
        read = self._source.read()
        age = self._age_seconds(read)
        bound = overlay.marks_source.max_age_seconds
        if age is not None and age <= bound:
            return self._from_source_marks(request, overlay, read, age)
        if age is None:
            why = read.detail or "the marks source could not be read"
        else:
            why = f"source marks are {age} s old, over the {bound} s bound"
        return self._unsynced(request, why)

    @staticmethod
    def _declares(host: str, overlay: ModelRotationOverlay) -> bool:
        """Whether the overlay declares ``host``: its own pool file is then authoritative.

        A host answers to its overlay name or, for the marks source, to the address the overlay dials it
        by (the source's own native hostname is not its overlay name), so the source never reads itself over ssh.
        """
        source = overlay.marks_source
        source_address = (source.ssh_target or "").split(".", 1)[0]
        return (
            host in {h.name for h in overlay.hosts}
            or host == source.host
            or (source_address != "" and host == source_address)
        )

    def _status(self) -> ModelClaudeTokenPoolResult:
        """The pool as it stands on disk; a dry run reads and writes nothing."""
        return self._pool.handle(
            ModelClaudeTokenPoolRequest(operation="status", dry_run=True)
        )

    @staticmethod
    def _labels_of(pooled: ModelClaudeTokenPoolResult) -> tuple[str, ...]:
        return tuple(entry.label for entry in pooled.tokens)

    @staticmethod
    def _limited_of(pooled: ModelClaudeTokenPoolResult) -> tuple[str, ...]:
        return tuple(entry.label for entry in pooled.tokens if entry.limited)

    def _from_local_pool(
        self, request: ModelPoolStatusViewRequest
    ) -> ModelPoolStatusViewResult:
        status = self._status()
        return ModelPoolStatusViewResult(
            host=request.host,
            basis="local_pool",
            marks_source_host=None,
            marks_age_seconds=None,
            local_copy_age_seconds=None,
            tokens=len(status.tokens),
            labels=self._labels_of(status),
            limited=self._limited_of(status),
            revoked=status.revoked,
            challenged=status.challenged,
            status_line=status.status_line,
            **self._labels(request, status, f"basis=local_pool host={request.host}"),
        )

    def _from_source_marks(
        self,
        request: ModelPoolStatusViewRequest,
        overlay: ModelRotationOverlay,
        read: ModelRotationMarksRead,
        age_seconds: int,
    ) -> ModelPoolStatusViewResult:
        """The pool with the source's fresh marks applied to a view; the dry run writes nothing."""
        pooled = self._pool.handle(
            ModelClaudeTokenPoolRequest(
                operation="sync_marks",
                marks=read.marks,
                marks_at=read.at,
                dry_run=True,
            )
        )
        source_host = overlay.marks_source.host
        return ModelPoolStatusViewResult(
            host=request.host,
            basis="source_marks",
            marks_source_host=source_host,
            marks_age_seconds=age_seconds,
            local_copy_age_seconds=None,
            tokens=len(pooled.tokens),
            labels=self._labels_of(pooled),
            limited=self._limited_of(pooled),
            revoked=pooled.revoked,
            challenged=pooled.challenged,
            status_line=(
                f"{pooled.status_line} marks=source:{source_host} "
                f"marks_age_s={age_seconds}"
            ),
            **self._labels(
                request,
                pooled,
                f"basis=source_marks source={source_host} marks_age_s={age_seconds}",
            ),
        )

    def _labels(
        self,
        request: ModelPoolStatusViewRequest,
        pooled: ModelClaudeTokenPoolResult,
        basis: str,
    ) -> dict[str, object]:
        """Per label: its limit state, the reset it reads and the hosts whose fresh login record names it."""
        overlay = self._overlay
        logins = self._board.read() if self._board is not None else None
        declared = tuple(h.name for h in overlay.hosts) if overlay is not None else ()
        bound = (
            3 * overlay.schedule.interval_seconds if overlay is not None else 0
        )  # three ticks: one missed tick is not yet a stale record
        now = self._now()
        placed: dict[str, list[str]] = {}
        seen: set[str] = set()
        for login in logins or ():
            if (now - login.at).total_seconds() > bound or login.host in seen:
                continue
            seen.add(login.host)
            if login.label is not None:
                placed.setdefault(login.label, []).append(login.host)
        unread = tuple(h for h in declared if h not in seen)
        states: list[ModelPoolLabelState] = []
        for entry in pooled.tokens:
            state = (
                (entry.revoke_cause or "revoked")
                if entry.revoked
                else "limited"
                if entry.limited
                else "usable"
            )
            states.append(
                ModelPoolLabelState(
                    label=entry.label,
                    state=state,
                    reset=entry.until if state == "limited" else None,
                    hosts=tuple(sorted(placed.get(entry.label, ()))),
                )
            )
        rows = " ".join(
            f"{s.label}:{s.state}:reset={s.reset or 'none'}:hosts={','.join(s.hosts) or 'none'}"
            for s in states
        )
        board = "unreadable" if logins is None else f"{len(seen)}/{len(declared)}"
        line = (
            f"claude-pool-labels {basis} labels={len(states)} logins_read={board}"
            + (f" hosts_unread={','.join(unread)}" if unread else "")
            + (f" | {rows}" if rows else "")
        )
        return {
            "label_states": tuple(states),
            "labels_line": line,
            "hosts_unread": unread,
        }

    def _age_seconds(self, read: ModelRotationMarksRead) -> int | None:
        """The read's age in seconds, ``None`` when the read gave no instant to age against."""
        if read.state != "read" or read.at is None:
            return None
        return int((self._now() - read.at).total_seconds())

    def _unsynced(
        self, request: ModelPoolStatusViewRequest, why: str
    ) -> ModelPoolStatusViewResult:
        """The local copy alone: labels only — no limited or revoked label is named from it."""
        status = self._status()
        written_at = self._copy_written_at()
        copy_age = (
            int((self._now() - written_at).total_seconds())
            if written_at is not None
            else None
        )
        tokens = len(status.tokens)
        return ModelPoolStatusViewResult(
            host=request.host,
            basis="unsynced_local_copy",
            marks_source_host=None,
            marks_age_seconds=None,
            local_copy_age_seconds=copy_age,
            detail=why,
            tokens=tokens,
            labels=self._labels_of(status),
            limited=None,
            revoked=None,
            challenged=None,
            status_line=(
                f"claude-pool host={request.host} tokens={tokens} UNSYNCED "
                f"local copy age_s={copy_age if copy_age is not None else 'unknown'} marks not read ({why}); "
                "limited and revoked labels are not named from it"
            ),
            labels_line=(
                f"claude-pool-labels basis=unsynced_local_copy labels={tokens} "
                f"marks not read ({why}); no label's limit state, reset or hosts is named from the local copy"
            ),
        )
