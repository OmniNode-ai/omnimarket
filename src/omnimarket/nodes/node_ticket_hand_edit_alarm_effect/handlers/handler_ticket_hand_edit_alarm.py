# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A hand edit of a standard ticket in Linear raises a typed error and an alarm (OMN-23776).

Two messages reach the node. A written event of the ticket node is kept as the ticket's
newest write (the newer Linear updatedAt wins) and closes the ticket's open findings whose
found content it matches. The runtime heartbeat, at most once per the overlay's
``check_interval_seconds``, reads the issues Linear changed after the node's cursor and
compares each standard ticket with its newest write. Each finding is one typed error event,
one slack-publish command under the same idempotency key and one open-findings row; a key
already open is not raised again. An unreadable source is ``detector_unreadable`` with an
alarm, and the cursor never moves past what was not read.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import yaml

from omnimarket.config.service_endpoints import service_endpoint_url
from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.inference.secret_store_resolver import resolve_api_key_loop_safe
from omnimarket.models.model_ticket_contract_written import ModelTicketContractWritten
from omnimarket.nodes.contract_topics import contract_secret_ref, contract_topic
from omnimarket.nodes.node_ticket_hand_edit_alarm_effect.handlers.compare_ticket import (
    compare_ticket,
    editors_after,
)
from omnimarket.nodes.node_ticket_hand_edit_alarm_effect.handlers.linear_issue_reader import (
    LinearIssueGateway,
)
from omnimarket.nodes.node_ticket_hand_edit_alarm_effect.handlers.store_ticket_hand_edit import (
    TicketHandEditStore,
)
from omnimarket.nodes.node_ticket_hand_edit_alarm_effect.models import (
    EnumTicketHandEditFindingKind,
    EnumTicketHandEditOutcome,
    ModelLinearIssueSnapshot,
    ModelTicketHandEditFinding,
    ModelTicketHandEditResult,
    ModelTicketHandEditSettings,
    ModelTicketHandEditSlackCommand,
    ModelTicketHandEditSweepTrigger,
)
from omnimarket.nodes.node_ticket_hand_edit_alarm_effect.overlay import load_settings
from omnimarket.nodes.node_ticket_hand_edit_alarm_effect.protocols import (
    ProtocolLinearIssueReader,
)
from omnimarket.protocols.protocol_projection_database_sync import DatabaseAdapter

logger = logging.getLogger(__name__)
_CONTRACT_PATH = Path(__file__).parent.parent / "contract.yaml"
_CHANNEL_SECRET = "SLACK_CHANNEL_ID"
_LINEAR_SECRET = "LINEAR_API_KEY"
_SLACK_SUFFIX = "slack-publish.v1"
_FINDING_SUFFIX = "ticket-hand-edit-finding.v1"
_HAND_EDIT = EnumTicketHandEditFindingKind.TICKET_HAND_EDIT
_UNREADABLE = EnumTicketHandEditFindingKind.DETECTOR_UNREADABLE


def hand_edit_key(ticket_id: str, content_sha256: str) -> str:
    """One finding per ticket and found content: the ticket id plus the found content hash."""
    return f"ticket_hand_edit:{ticket_id}:{content_sha256}"


def unreadable_key(source: str, cursor: datetime) -> str:
    """One alarm per unread source and cursor, so a repeated failure is not re-raised."""
    return f"detector_unreadable:{source}:{cursor.isoformat()}"


class _SourceUnreadError(Exception):
    """One source could not be read; carries the finding's source label."""

    def __init__(self, source: str, error: BaseException) -> None:
        super().__init__(f"{source}: {type(error).__name__}: {error}")
        self.source = source


class HandlerTicketHandEditAlarm:
    """Reads the overlay, the store and Linear at evaluation time, never at wiring time."""

    def __init__(
        self,
        *,
        event_publisher: Callable[[str, bytes], None] | None = None,
        database: DatabaseAdapter | None = None,
        reader: ProtocolLinearIssueReader | None = None,
        clock: Callable[[], float] | None = None,
        contract_path: Path | None = None,
    ) -> None:
        self._contract_path = contract_path or _CONTRACT_PATH
        self._slack_topic = contract_topic(
            self._contract_path, "publish_topics", _SLACK_SUFFIX
        )
        self._finding_topic = contract_topic(
            self._contract_path, "publish_topics", _FINDING_SUFFIX
        )
        source = yaml.safe_load(self._contract_path.read_text(encoding="utf-8"))
        self._writes_dsn_env: str = source["writes_source"]["dsn_env"]
        self._writes_schema: str = source["writes_source"]["schema"]
        self._graphql_url_ref: str = source["integrations"]["linear"]["graphql_url_ref"]
        self._publisher = event_publisher
        self._database = database
        self._reader = reader
        self._clock = clock or time.time
        self._store: TicketHandEditStore | None = None
        self._last_swept_at: float | None = None

    # -- entries ---------------------------------------------------------------

    def handle(
        self, request: ModelTicketHandEditSweepTrigger | ModelTicketContractWritten
    ) -> ModelTicketHandEditResult:
        if isinstance(request, ModelTicketContractWritten):
            return self._on_written(request)
        return self._sweep(request)

    def _on_written(
        self, event: ModelTicketContractWritten
    ) -> ModelTicketHandEditResult:
        now = self._now()
        try:
            store = self._get_store()
            kept = store.record_written(event)
            closed = tuple(store.close_matching(event, now))
        except Exception as exc:
            reason = f"write record: {type(exc).__name__}: {exc}"
            logger.error("Ticket hand-edit write record unreadable: %s", reason)
            return ModelTicketHandEditResult(
                outcome=EnumTicketHandEditOutcome.UNREADABLE, reason=reason
            )
        return ModelTicketHandEditResult(
            outcome=EnumTicketHandEditOutcome.RECORDED
            if kept or closed
            else EnumTicketHandEditOutcome.IGNORED,
            findings_closed=closed,
        )

    def _sweep(
        self, trigger: ModelTicketHandEditSweepTrigger
    ) -> ModelTicketHandEditResult:
        epoch = self._clock()
        try:
            settings = load_settings()
        except NodeOverlayError as exc:
            logger.error("Ticket hand-edit alarm refused: %s", exc)
            return ModelTicketHandEditResult(
                outcome=EnumTicketHandEditOutcome.REFUSED, reason=str(exc)
            )
        last = self._last_swept_at
        # A clock that stepped back (elapsed < 0) is not a reason to stay throttled.
        if last is not None and 0 <= epoch - last < settings.check_interval_seconds:
            return ModelTicketHandEditResult(
                outcome=EnumTicketHandEditOutcome.THROTTLED
            )
        self._last_swept_at = epoch
        now = datetime.fromtimestamp(epoch, tz=UTC)
        correlation_id = trigger.correlation_id or uuid4()
        fallback_cursor = now - timedelta(seconds=settings.initial_lookback_seconds)
        try:
            store = self._get_store()
            cursor = store.cursor() or fallback_cursor
        except Exception as exc:
            return self._unreadable(
                _SourceUnreadError("write record", exc),
                fallback_cursor,
                now,
                correlation_id,
                None,
            )
        try:
            issues = sorted(
                self._get_reader().issues_updated_after(
                    cursor, max_pages=settings.max_pages
                ),
                key=lambda issue: issue.updated_at,
            )
        except Exception as exc:
            return self._unreadable(
                _SourceUnreadError("linear issues", exc),
                cursor,
                now,
                correlation_id,
                store,
            )

        raised: list[str] = []
        already: list[str] = []
        errors: list[str] = []
        unread_at: datetime | None = None
        for issue in issues:
            try:
                finding = self._judge(issue, store, settings, now, correlation_id)
            except _SourceUnreadError as exc:
                unread_at = unread_at or issue.updated_at
                finding = self._unread_finding(exc, cursor, now, correlation_id)
            if finding is None:
                continue
            error = self._raise(finding, store, raised, already)
            if error is not None:
                errors.append(error)
                unread_at = unread_at or issue.updated_at
        cursor_after = _advance(cursor, issues, unread_at)
        if cursor_after != cursor:
            try:
                store.move_cursor(cursor_after)
            except Exception as exc:
                errors.append(f"cursor: {type(exc).__name__}: {exc}")
                cursor_after = cursor
        return ModelTicketHandEditResult(
            outcome=EnumTicketHandEditOutcome.EVALUATED
            if unread_at is None
            else EnumTicketHandEditOutcome.UNREADABLE,
            issues_read=len(issues),
            findings_raised=tuple(raised),
            findings_already_open=tuple(already),
            cursor_before=cursor,
            cursor_after=cursor_after,
            error="; ".join(errors) or None,
        )

    # -- judging ---------------------------------------------------------------

    def _judge(
        self,
        issue: ModelLinearIssueSnapshot,
        store: TicketHandEditStore,
        settings: ModelTicketHandEditSettings,
        now: datetime,
        correlation_id: UUID,
    ) -> ModelTicketHandEditFinding | None:
        try:
            written = store.newest_written(issue.identifier)
        except Exception as exc:
            raise _SourceUnreadError(
                f"write record of {issue.identifier}", exc
            ) from exc
        comparison = compare_ticket(issue, written, settings.normalizations)
        if (
            written is None
            and comparison.fingerprint_found is None
            and not comparison.differs
        ):
            return None  # not a standard ticket, and not one the node wrote
        differs = list(comparison.differs)
        editors: tuple[str, ...] = ()
        if differs or settings.node_actor_id:
            if issue.document_content_id is None:
                if differs:
                    differs.append("the description history names no document")
            else:
                try:
                    revisions = self._get_reader().description_revisions(
                        issue.document_content_id
                    )
                except Exception as exc:
                    raise _SourceUnreadError(
                        f"description history of {issue.identifier}", exc
                    ) from exc
                editors = editors_after(revisions, written)
            if settings.node_actor_id:
                others = [actor for actor in editors if actor != settings.node_actor_id]
                if editors and not others:
                    # Every revision since the newest write record is the node's own:
                    # its written event is still in flight, not a hand edit.
                    return None
                if others:
                    differs.append(
                        "description revised by "
                        + ", ".join(others)
                        + ", not the ticket node"
                    )
        if not differs:
            return None
        return ModelTicketHandEditFinding(
            kind=_HAND_EDIT,
            idempotency_key=hand_edit_key(
                issue.identifier, comparison.content_sha256_found
            ),
            ticket_id=issue.identifier,
            fingerprint_written=(written.contract_fingerprint or None)
            if written
            else None,
            fingerprint_found=comparison.fingerprint_found,
            content_sha256_written=written.content_sha256 if written else None,
            content_sha256_found=comparison.content_sha256_found,
            differs=tuple(differs),
            editors=editors,
            detected_at=now,
            correlation_id=correlation_id,
        )

    def _unread_finding(
        self,
        exc: _SourceUnreadError,
        cursor: datetime,
        now: datetime,
        correlation_id: UUID,
    ) -> ModelTicketHandEditFinding:
        logger.error("Ticket hand-edit source unreadable: %s", exc)
        return ModelTicketHandEditFinding(
            kind=_UNREADABLE,
            idempotency_key=unreadable_key(exc.source, cursor),
            reason=str(exc),
            detected_at=now,
            correlation_id=correlation_id,
        )

    def _unreadable(
        self,
        exc: _SourceUnreadError,
        cursor: datetime,
        now: datetime,
        correlation_id: UUID,
        store: TicketHandEditStore | None,
    ) -> ModelTicketHandEditResult:
        finding = self._unread_finding(exc, cursor, now, correlation_id)
        raised: list[str] = []
        already: list[str] = []
        error = self._raise(finding, store, raised, already)
        return ModelTicketHandEditResult(
            outcome=EnumTicketHandEditOutcome.UNREADABLE,
            reason=str(exc),
            findings_raised=tuple(raised),
            findings_already_open=tuple(already),
            cursor_before=cursor,
            cursor_after=cursor,
            error=error,
        )

    # -- raising ---------------------------------------------------------------

    def _raise(
        self,
        finding: ModelTicketHandEditFinding,
        store: TicketHandEditStore | None,
        raised: list[str],
        already: list[str],
    ) -> str | None:
        """Publish the error and the command, then open the row; an open key is not raised again."""
        key = finding.idempotency_key
        try:
            if store is not None and store.is_open(key):
                already.append(key)
                return None
        except Exception as exc:
            logger.error("Open findings unreadable: %s", exc)
        publisher = self._publisher
        if publisher is None:
            error = f"{key}: no event_publisher was injected; nothing was published"
            logger.error("Ticket hand-edit alarm NOT published: %s", error)
            return error
        try:
            publisher(self._finding_topic, finding.model_dump_json().encode())
            ref = contract_secret_ref(self._contract_path, _CHANNEL_SECRET)
            secret = resolve_api_key_loop_safe(ref, env_var_fallback=ref)
            if secret is None:
                raise RuntimeError(f"secret ref {ref!r} resolved to None")
            command = ModelTicketHandEditSlackCommand(
                channel=secret.get_secret_value(),
                text=finding.text(),
                idempotency_key=key,
                correlation_id=finding.correlation_id,
            )
            publisher(self._slack_topic, command.model_dump_json().encode())
            if store is not None:
                store.open_finding(finding)
        except Exception as exc:
            error = f"{key}: {type(exc).__name__}: {exc}"
            logger.error("Ticket hand-edit alarm NOT fully published: %s", error)
            return error
        raised.append(key)
        return None

    # -- boundaries ------------------------------------------------------------

    def _now(self) -> datetime:
        return datetime.fromtimestamp(self._clock(), tz=UTC)

    def _get_store(self) -> TicketHandEditStore:
        if self._store is None:
            if self._database is None:
                # Resolved at first use, so an absent DSN becomes detector_unreadable,
                # never a wiring failure.
                from omnimarket.projection.postgres_sync_database import (
                    PostgresSyncProjectionAdapter,
                )

                dsn = os.environ.get(self._writes_dsn_env, "")
                if not dsn.strip():
                    raise RuntimeError(f"{self._writes_dsn_env} is unset or empty")
                self._database = PostgresSyncProjectionAdapter(
                    dsn, schema=self._writes_schema
                )
            self._store = TicketHandEditStore(self._database)
        return self._store

    def _get_reader(self) -> ProtocolLinearIssueReader:
        if self._reader is None:
            ref = contract_secret_ref(self._contract_path, _LINEAR_SECRET)
            secret = resolve_api_key_loop_safe(ref, env_var_fallback=ref)
            if secret is None:
                raise RuntimeError(f"secret ref {ref!r} resolved to None")
            self._reader = LinearIssueGateway(
                api_key=secret.get_secret_value(),
                graphql_url=service_endpoint_url(self._graphql_url_ref),
            )
        return self._reader


def _advance(
    cursor: datetime,
    issues: list[ModelLinearIssueSnapshot],
    unread_at: datetime | None,
) -> datetime:
    """The newest updatedAt read in full; never past an issue that was not read."""
    done = [
        issue.updated_at
        for issue in issues
        if unread_at is None or issue.updated_at < unread_at
    ]
    position = max(done, default=cursor)
    return max(position, cursor)


__all__ = ["HandlerTicketHandEditAlarm", "hand_edit_key", "unreadable_key"]
