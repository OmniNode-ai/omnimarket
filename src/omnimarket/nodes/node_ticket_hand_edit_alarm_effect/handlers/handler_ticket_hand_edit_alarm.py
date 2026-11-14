# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Linear changed issues in, ticket_hand_edit out: a standard ticket changed outside the node raises an error and an alarm."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import yaml

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.inference.secret_store_resolver import resolve_api_key_loop_safe
from omnimarket.nodes.contract_topics import contract_secret_ref, contract_topic
from omnimarket.nodes.node_ticket_hand_edit_alarm_effect.handlers.compare_ticket import (
    NO_FINGERPRINT,
    content_sha256,
    differences_from_record,
    editors_after_write,
    read_body,
)
from omnimarket.nodes.node_ticket_hand_edit_alarm_effect.handlers.linear_issue_source import (
    LinearIssueSourceGateway,
)
from omnimarket.nodes.node_ticket_hand_edit_alarm_effect.handlers.store_ticket_hand_edit import (
    TicketHandEditStore,
)
from omnimarket.nodes.node_ticket_hand_edit_alarm_effect.models import (
    EnumTicketHandEditKind,
    EnumTicketHandEditOutcome,
    ModelLinearIssueSnapshot,
    ModelTicketContractWritten,
    ModelTicketHandEditFinding,
    ModelTicketHandEditInput,
    ModelTicketHandEditResult,
    ModelTicketHandEditSettings,
    ModelTicketHandEditSlackCommand,
    ModelTicketHandEditSweepTrigger,
    ModelTicketWriteRecord,
)
from omnimarket.nodes.node_ticket_hand_edit_alarm_effect.overlay import load_settings
from omnimarket.nodes.node_ticket_hand_edit_alarm_effect.protocols.protocol_linear_issue_source import (
    ProtocolLinearIssueSource,
)
from omnimarket.protocols.protocol_projection_database_sync import DatabaseAdapter

logger = logging.getLogger(__name__)
_CONTRACT_PATH = Path(__file__).parent.parent / "contract.yaml"
_CHANNEL_SECRET = "SLACK_CHANNEL_ID"
_SLACK_SUFFIX = "slack-publish.v1"
_FINDING_SUFFIX = "ticket-hand-edit.v1"
_UNREADABLE_TICKET = "detector:linear"
_READ_LIST, _READ_HISTORY, _READ_RECORD = (
    "linear_issue_list",
    "linear_description_history",
    "write_record_store",
)


class _SourceUnreadableError(Exception):
    """A read this sweep needs failed; the message names which one."""

    def __init__(self, source: str, cause: Exception) -> None:
        super().__init__(f"{source}: {type(cause).__name__}: {cause}")
        self.source = source


class HandlerTicketHandEditAlarm:
    """Reads the overlay, the write record and Linear at evaluation time, never at wiring time."""

    def __init__(
        self,
        *,
        event_publisher: Callable[[str, bytes], None] | None = None,
        database: DatabaseAdapter | None = None,
        source: ProtocolLinearIssueSource | None = None,
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
        declared = yaml.safe_load(self._contract_path.read_text(encoding="utf-8"))
        self._dsn_env: str = declared["writes_source"]["dsn_env"]
        self._schema: str = declared["writes_source"]["schema"]
        self._publisher = event_publisher
        self._database = database
        self._source = source
        self._clock = clock or time.time
        self._store: TicketHandEditStore | None = None
        self._last_sweep_at: float | None = None
        self._unreadable_alarmed: set[str] = set()
        self._lock = threading.Lock()

    def handle(self, request: ModelTicketHandEditInput) -> ModelTicketHandEditResult:
        try:
            settings = load_settings()
        except NodeOverlayError as exc:
            logger.error("Ticket hand edit alarm refused: %s", exc)
            return ModelTicketHandEditResult(
                outcome=EnumTicketHandEditOutcome.REFUSED, reason=str(exc)
            )
        now = datetime.fromtimestamp(self._clock(), tz=UTC)
        with self._lock:
            if isinstance(request, ModelTicketHandEditSweepTrigger):
                return self._sweep(settings, now)
            return self._on_written(request, now)

    # -- the write record -------------------------------------------------------

    def _on_written(
        self, event: ModelTicketContractWritten, now: datetime
    ) -> ModelTicketHandEditResult:
        """Keep the newest written event; a later one that matches a finding's content closes it."""
        try:
            store = self._get_store()
            if not store.record_written(event, now):
                return ModelTicketHandEditResult(
                    outcome=EnumTicketHandEditOutcome.STALE_WRITE_IGNORED
                )
            closed = 0
            for row in store.open_findings_for(event.ticket_id):
                if row["found_content_sha256"] == event.content_sha256:
                    store.close_finding(
                        str(row["finding_key"]),
                        now,
                        f"written event {event.content_sha256} at {event.linear_updated_at.isoformat()}",
                    )
                    closed += 1
        except Exception as exc:
            logger.error(
                "Ticket write record NOT kept for %s: %s", event.ticket_id, exc
            )
            return ModelTicketHandEditResult(
                outcome=EnumTicketHandEditOutcome.UNREADABLE,
                error=f"{type(exc).__name__}: {exc}",
            )
        return ModelTicketHandEditResult(
            outcome=EnumTicketHandEditOutcome.WRITE_RECORDED, closed=closed
        )

    # -- the sweep --------------------------------------------------------------

    def _sweep(
        self, settings: ModelTicketHandEditSettings, now: datetime
    ) -> ModelTicketHandEditResult:
        started = now.timestamp()
        if (
            self._last_sweep_at is not None
            and started - self._last_sweep_at < settings.check_interval_seconds
        ):
            return ModelTicketHandEditResult(
                outcome=EnumTicketHandEditOutcome.THROTTLED
            )
        self._last_sweep_at = started
        findings: list[ModelTicketHandEditFinding] = []
        closed = checked = 0
        try:
            store = self._get_store()
            cursor = self._guard(_READ_RECORD, store.cursor) or now - timedelta(
                seconds=settings.initial_lookback_seconds
            )
            issues = self._guard(
                _READ_LIST, lambda: self._get_source().list_issues_updated_after(cursor)
            )
            # A write's event can trail the write by a moment: an issue changed within the grace
            # window is judged by a later sweep, and the cursor stops before it.
            settled = now - timedelta(seconds=settings.write_grace_seconds)
            newest = cursor
            for issue in sorted(issues, key=lambda i: i.updated_at):
                if issue.updated_at > settled:
                    break
                judged = self._check_issue(store, settings, issue, now)
                checked += 1
                closed += judged[1]
                if judged[0] is not None:
                    findings.append(judged[0])
                newest = max(newest, issue.updated_at)
            errors = [
                error
                for finding in findings
                if (error := self._raise(store, finding, now)) is not None
            ]
            if errors:
                # The cursor stays: the next sweep reads the same issues again and the
                # open-findings row, written last, keeps a delivered alarm from repeating.
                raise _SourceUnreadableError(
                    "alarm_delivery", RuntimeError("; ".join(errors))
                )
            self._guard(_READ_RECORD, lambda: store.set_cursor(newest, now))
        except _SourceUnreadableError as exc:
            return self._unreadable(exc, now, findings=tuple(findings), checked=checked)
        except Exception as exc:
            return self._unreadable(
                _SourceUnreadableError(_READ_RECORD, exc),
                now,
                findings=tuple(findings),
                checked=checked,
            )
        self._unreadable_alarmed.clear()
        self._close_unreadable(store, now)
        return ModelTicketHandEditResult(
            outcome=EnumTicketHandEditOutcome.SWEPT,
            findings=tuple(findings),
            checked=checked,
            closed=closed,
        )

    def _check_issue(
        self,
        store: TicketHandEditStore,
        settings: ModelTicketHandEditSettings,
        issue: ModelLinearIssueSnapshot,
        now: datetime,
    ) -> tuple[ModelTicketHandEditFinding | None, int]:
        """One issue: its finding if any, and how many open findings its matching content closed."""
        record = self._guard(
            _READ_RECORD, lambda: store.newest_written(issue.ticket_id)
        )
        found_hash = content_sha256(issue.title, issue.description)
        reading = read_body(issue.description, settings.normalizations)
        differences = differences_from_record(found_hash, record, reading)
        if record is None and not differences:
            return None, 0
        editors: tuple[str, ...] = ()
        if differences or settings.node_actor_id is not None:
            editors = editors_after_write(
                self._guard(
                    _READ_HISTORY,
                    lambda: self._get_source().description_revisions(issue),
                ),
                record,
                settings.node_actor_id,
            )
            if not differences and editors:
                differences = (
                    "the description was revised after the node's last write by an actor other than the node's identity",
                )
        if not differences:
            return None, self._close_matching(store, issue.ticket_id, now)
        key = f"{issue.ticket_id}:{found_hash}"
        if self._guard(_READ_RECORD, lambda: store.is_open(key)):
            return None, 0
        return (
            _finding(
                issue,
                record,
                found_hash,
                reading.found_fingerprint,
                differences,
                editors,
                key,
            ),
            0,
        )

    def _close_matching(
        self, store: TicketHandEditStore, ticket_id: str, now: datetime
    ) -> int:
        def close_all() -> int:
            rows = store.open_findings_for(ticket_id)
            for row in rows:
                store.close_finding(
                    str(row["finding_key"]),
                    now,
                    "the ticket again equals the newest written event",
                )
            return len(rows)

        return self._guard(_READ_RECORD, close_all)

    # -- raising ----------------------------------------------------------------

    def _raise(
        self,
        store: TicketHandEditStore,
        finding: ModelTicketHandEditFinding,
        now: datetime,
    ) -> str | None:
        """Event, command, then the open row; each step is tried so one failure never hides the others."""
        errors: list[str] = []
        self._attempt(
            errors,
            "finding event",
            lambda: self._publish(self._finding_topic, finding.model_dump_json()),
        )
        self._attempt(errors, "slack command", lambda: self._publish_slack(finding))
        if not errors:
            self._attempt(
                errors, "open finding row", lambda: store.open_finding(finding, now)
            )
        if errors:
            logger.error(
                "Ticket hand edit finding NOT fully delivered: %s error=%s",
                finding.ticket_id,
                "; ".join(errors),
            )
        return "; ".join(errors) or None

    def _unreadable(
        self,
        exc: _SourceUnreadableError,
        now: datetime,
        *,
        findings: tuple[ModelTicketHandEditFinding, ...],
        checked: int,
    ) -> ModelTicketHandEditResult:
        """A source this sweep needs failed: one alarm per source until a sweep reads clean; the cursor stays."""
        logger.error("Ticket hand edit detector unreadable: %s", exc)
        reason = str(exc)
        finding = ModelTicketHandEditFinding(
            kind=EnumTicketHandEditKind.DETECTOR_UNREADABLE,
            ticket_id=_UNREADABLE_TICKET,
            reason=reason,
            text=f"The ticket hand edit detector cannot read {exc.source}, so hand edits of standard tickets are not being checked. {reason}",
            idempotency_key=f"detector_unreadable:{exc.source}:{now:%Y%m%dT%H%M%SZ}",
            correlation_id=uuid4(),
        )
        error: str | None = None
        if exc.source not in self._unreadable_alarmed:
            self._unreadable_alarmed.add(exc.source)
            errors: list[str] = []
            self._attempt(
                errors,
                "unreadable event",
                lambda: self._publish(self._finding_topic, finding.model_dump_json()),
            )
            self._attempt(errors, "slack command", lambda: self._publish_slack(finding))
            self._attempt(
                errors,
                "open finding row",
                lambda: self._get_store().open_finding(finding, now),
            )
            error = "; ".join(errors) or None
            if errors:
                self._unreadable_alarmed.discard(exc.source)
        return ModelTicketHandEditResult(
            outcome=EnumTicketHandEditOutcome.UNREADABLE,
            reason=reason,
            findings=(*findings, finding),
            checked=checked,
            error=error,
        )

    def _close_unreadable(self, store: TicketHandEditStore, now: datetime) -> None:
        try:
            for row in store.open_findings_for(_UNREADABLE_TICKET):
                store.close_finding(
                    str(row["finding_key"]), now, "a later sweep read every source"
                )
        except Exception as exc:
            logger.warning("Open unreadable findings NOT closed: %s", exc)

    # -- boundaries -------------------------------------------------------------

    @staticmethod
    def _guard[T](source: str, read: Callable[[], T]) -> T:
        try:
            return read()
        except _SourceUnreadableError:
            raise
        except Exception as exc:
            raise _SourceUnreadableError(source, exc) from exc

    @staticmethod
    def _attempt(errors: list[str], step: str, action: Callable[[], object]) -> bool:
        try:
            action()
        except Exception as exc:
            errors.append(f"{step}: {type(exc).__name__}: {exc}")
            return False
        return True

    def _publish(self, topic: str, payload_json: str) -> None:
        if self._publisher is None:
            raise RuntimeError("no event_publisher was injected; nothing was published")
        self._publisher(topic, payload_json.encode())

    def _publish_slack(self, finding: ModelTicketHandEditFinding) -> None:
        ref = contract_secret_ref(self._contract_path, _CHANNEL_SECRET)
        secret = resolve_api_key_loop_safe(ref, env_var_fallback=ref)
        if secret is None:
            raise RuntimeError(f"secret ref {ref!r} resolved to None")
        command = ModelTicketHandEditSlackCommand(
            channel=secret.get_secret_value(),
            text=finding.text,
            idempotency_key=finding.idempotency_key,
            correlation_id=finding.correlation_id,
        )
        self._publish(self._slack_topic, command.model_dump_json())

    def _get_store(self) -> TicketHandEditStore:
        if self._store is None:
            if self._database is None:
                # Resolved at first use, so an absent DSN becomes detector_unreadable or a
                # refusal, never a wiring failure.
                import os

                from omnimarket.projection.postgres_sync_database import (
                    PostgresSyncProjectionAdapter,
                )

                dsn = os.environ.get(self._dsn_env, "")
                if not dsn.strip():
                    raise RuntimeError(f"{self._dsn_env} is unset or empty")
                self._database = PostgresSyncProjectionAdapter(dsn, schema=self._schema)
            self._store = TicketHandEditStore(self._database)
        return self._store

    def _get_source(self) -> ProtocolLinearIssueSource:
        if self._source is None:
            self._source = LinearIssueSourceGateway()
        return self._source


def _finding(
    issue: ModelLinearIssueSnapshot,
    record: ModelTicketWriteRecord | None,
    found_hash: str,
    found_fingerprint: str,
    differences: tuple[str, ...],
    editors: tuple[str, ...],
    key: str,
) -> ModelTicketHandEditFinding:
    written = record.contract_fingerprint if record is not None else ""
    found = found_fingerprint or NO_FINGERPRINT
    who = (
        ", ".join(editors)
        or "unknown (no revision after the node's last write was read)"
    )
    return ModelTicketHandEditFinding(
        kind=EnumTicketHandEditKind.TICKET_HAND_EDIT,
        ticket_id=issue.ticket_id,
        written_fingerprint=written,
        found_fingerprint=found,
        written_content_sha256=record.content_sha256 if record is not None else "",
        found_content_sha256=found_hash,
        differences=differences,
        editors=editors,
        text=(
            f"{issue.ticket_id} was edited by hand in Linear. The node wrote fingerprint "
            f"{written or 'none'}; the ticket now reads {found}. Editors: {who}. "
            f"Differences: {'; '.join(differences)}. Take the ticket's side with onex ticket pull."
        ),
        idempotency_key=key,
        correlation_id=uuid4(),
    )


__all__ = ["HandlerTicketHandEditAlarm"]
