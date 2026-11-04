# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Heartbeat-carried stall evaluation: gather lanes, judge them, emit per finding."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import yaml

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.inference.secret_store_resolver import resolve_api_key_loop_safe
from omnimarket.models.model_work_progress import (
    ModelWorkProgressFinding,
    ModelWorkProgressRequest,
)
from omnimarket.nodes.contract_topics import contract_secret_ref, contract_topic
from omnimarket.nodes.node_work_progress_alert_effect.handlers.gather_lanes import (
    gather_subjects,
)
from omnimarket.nodes.node_work_progress_alert_effect.handlers.ledger_row_reader import (
    PostgresLedgerRowReader,
    resolve_dsn,
)
from omnimarket.nodes.node_work_progress_alert_effect.handlers.protocol_ledger_row_reader import (
    ProtocolLedgerRowReader,
)
from omnimarket.nodes.node_work_progress_alert_effect.models import (
    EnumWorkProgressOutcome,
    ModelLedgerRow,
    ModelWorkProgressAlertTrigger,
    ModelWorkProgressDelivery,
    ModelWorkProgressEvaluated,
    ModelWorkProgressSlackCommand,
    ModelWorkProgressStalled,
)
from omnimarket.nodes.node_work_progress_alert_effect.overlay import load_settings
from omnimarket.nodes.node_work_progress_compute import NodeWorkProgressCompute
from omnimarket.nodes.node_work_progress_compute.overlay import (
    load_budgets,
    load_phase_notes,
)

logger = logging.getLogger(__name__)
_CONTRACT_PATH = Path(__file__).parent.parent / "contract.yaml"
_CHANNEL_SECRET = "SLACK_CHANNEL_ID"
_SLACK_SUFFIX = "slack-publish.v1"
_STALLED_SUFFIX = "work-progress-stalled.v1"


class HandlerWorkProgressAlert:
    """Reads both overlays and the ledger at evaluation time, never at wiring time."""

    def __init__(
        self,
        *,
        event_publisher: Callable[[str, bytes], None] | None = None,
        reader: ProtocolLedgerRowReader | None = None,
        clock: Callable[[], float] | None = None,
        contract_path: Path | None = None,
    ) -> None:
        self._contract_path = contract_path or _CONTRACT_PATH
        self._slack_topic = contract_topic(
            self._contract_path, "publish_topics", _SLACK_SUFFIX
        )
        self._stalled_topic = contract_topic(
            self._contract_path, "publish_topics", _STALLED_SUFFIX
        )
        source = yaml.safe_load(self._contract_path.read_text(encoding="utf-8"))
        self._dsn_env: str = source["reads_source"]["dsn_env"]
        self._publisher = event_publisher
        self._reader = reader
        self._clock = clock or time.time
        self._compute = NodeWorkProgressCompute()
        self._last_evaluated_at: float | None = None

    def handle(
        self, request: ModelWorkProgressAlertTrigger
    ) -> ModelWorkProgressEvaluated:
        """Return the terminal event; at most one evaluation per interval.

        The throttle stores the time of the last evaluation and compares the
        elapsed time with the interval the overlay gives now, so changing the
        interval on a live node can never leave the stored state ahead of it.
        """
        epoch = self._clock()
        try:
            settings = load_settings()
        except NodeOverlayError as exc:
            logger.error("Work progress alert refused: %s", exc)
            return ModelWorkProgressEvaluated(
                outcome=EnumWorkProgressOutcome.REFUSED, reason=str(exc)
            )
        interval = settings.check_interval_seconds
        last = self._last_evaluated_at
        # A clock that stepped back (elapsed < 0) is not a reason to stay throttled.
        if last is not None and 0 <= epoch - last < interval:
            return ModelWorkProgressEvaluated(
                outcome=EnumWorkProgressOutcome.THROTTLED,
                check_interval_seconds=interval,
            )
        self._last_evaluated_at = epoch
        now = datetime.fromtimestamp(epoch, tz=UTC)
        try:
            budgets = load_budgets()
        except NodeOverlayError as exc:
            logger.error("Work progress alert refused: %s", exc)
            return ModelWorkProgressEvaluated(
                outcome=EnumWorkProgressOutcome.REFUSED,
                reason=str(exc),
                check_interval_seconds=interval,
            )
        try:
            rows = self._read(now - timedelta(seconds=settings.lookback_seconds))
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"
            logger.error("Work progress source unreadable: %s", reason)
            return ModelWorkProgressEvaluated(
                outcome=EnumWorkProgressOutcome.UNREADABLE,
                reason=reason,
                check_interval_seconds=interval,
            )
        subjects = gather_subjects(
            rows,
            now=now,
            lookback_seconds=settings.lookback_seconds,
            ignore=settings.ignore,
        )
        result = self._compute.handle(
            ModelWorkProgressRequest(
                now=now,
                budgets=budgets,
                phase_notes=load_phase_notes(),
                subjects=subjects,
            )
        )
        correlation_id = request.correlation_id or uuid4()
        deliveries = tuple(
            self._emit(finding, correlation_id) for finding in result.findings
        )
        return ModelWorkProgressEvaluated(
            outcome=EnumWorkProgressOutcome.EVALUATED,
            check_interval_seconds=interval,
            subjects=len(subjects),
            findings=len(result.findings),
            commands=sum(1 for delivery in deliveries if delivery.command_published),
            deliveries=deliveries,
        )

    def _read(self, claimed_since: datetime) -> tuple[ModelLedgerRow, ...]:
        if self._reader is None:
            # Resolved at the first evaluation, so an absent DSN becomes an
            # unreadable-source terminal event rather than a wiring failure.
            self._reader = PostgresLedgerRowReader(dsn=resolve_dsn(self._dsn_env))
        return tuple(self._reader.read(claimed_since))

    def _emit(
        self, finding: ModelWorkProgressFinding, correlation_id: UUID
    ) -> ModelWorkProgressDelivery:
        key = finding.idempotency_key
        stalled_published = command_published = False
        error: str | None = None
        publisher = self._publisher
        if publisher is None:
            error = "no event_publisher was injected; nothing was published"
        else:
            try:
                stalled = ModelWorkProgressStalled(
                    **finding.model_dump(), correlation_id=correlation_id
                )
                publisher(self._stalled_topic, stalled.model_dump_json().encode())
                stalled_published = True
                ref = contract_secret_ref(self._contract_path, _CHANNEL_SECRET)
                secret = resolve_api_key_loop_safe(ref, env_var_fallback=ref)
                if secret is None:
                    raise RuntimeError(f"secret ref {ref!r} resolved to None")
                command = ModelWorkProgressSlackCommand(
                    channel=secret.get_secret_value(),
                    text=finding.text,
                    idempotency_key=key,
                    correlation_id=correlation_id,
                )
                publisher(self._slack_topic, command.model_dump_json().encode())
                command_published = True
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
        if error is not None:
            logger.error("Stall alert NOT fully published: key=%s error=%s", key, error)
        return ModelWorkProgressDelivery(
            idempotency_key=key,
            stalled_published=stalled_published,
            command_published=command_published,
            error=error,
        )


__all__ = ["HandlerWorkProgressAlert"]
