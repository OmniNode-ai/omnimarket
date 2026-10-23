# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Timer-driven capture evaluation, canonical typed def-B (OMN-21340)."""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import socket
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from omnibase_core.protocols.runtime.protocol_local_runtime_bus import (
    ProtocolLocalRuntimeBus,
)

from omnimarket.inference.secret_store_resolver import resolve_api_key_loop_safe
from omnimarket.nodes.contract_topics import (
    contract_publish_topics,
    contract_secret_ref,
    contract_section,
)
from omnimarket.nodes.node_capture_silence_watch_effect.handlers.build_heartbeat import (
    build_heartbeat,
)
from omnimarket.nodes.node_capture_silence_watch_effect.handlers.build_slack_command import (
    build_slack_command,
)
from omnimarket.nodes.node_capture_silence_watch_effect.handlers.capture_watch_reader import (
    PostgresCaptureWatchReader,
    resolve_reads_source_dsn,
)
from omnimarket.nodes.node_capture_silence_watch_effect.handlers.classify_capture_silence import (
    classify_capture_silence,
)
from omnimarket.nodes.node_capture_silence_watch_effect.handlers.load_capture_watch_config import (
    load_capture_watch_config,
)
from omnimarket.nodes.node_capture_silence_watch_effect.handlers.protocol_capture_watch_reader import (
    ProtocolCaptureWatchReader,
)
from omnimarket.nodes.node_capture_silence_watch_effect.models import (
    CaptureWatchPolicyError,
    EnumCaptureWatchState,
    ModelCaptureObservation,
    ModelCaptureSource,
    ModelCaptureWatchDelivery,
    ModelCaptureWatchEvaluation,
    ModelCaptureWatchTrigger,
    ModelCaptureWatchVerdict,
)

logger = logging.getLogger(__name__)
_CONTRACT_PATH = Path(__file__).parent.parent / "contract.yaml"
_CHANNEL_SECRET = "SLACK_CHANNEL_ID"
_SECONDS_PER_MINUTE = 60


class HandlerCaptureSilenceWatch:
    """Self-assemble policy and read targets, then publish silence findings."""

    def __init__(
        self,
        *,
        event_publisher: Callable[[str, bytes], Awaitable[None] | None] | None = None,
        event_bus: ProtocolLocalRuntimeBus | None = None,
        reader: ProtocolCaptureWatchReader | None = None,
        clock: Callable[[], float] | None = None,
        contract_path: Path | None = None,
        host: str | None = None,
        pid: int | None = None,
    ) -> None:
        self._contract_path = contract_path or _CONTRACT_PATH
        self._policy, self._binding, self._sources = load_capture_watch_config(
            self._contract_path
        )
        try:
            topics = contract_publish_topics(self._contract_path)
            terminal = contract_section(self._contract_path, "terminal_event")
            if not isinstance(terminal, str) or not terminal:
                raise CaptureWatchPolicyError("contract must declare a terminal_event")
            self._slack_topic = self._topic(topics, "slack-publish")
            self._heartbeat_topic = self._topic(topics, "automation-heartbeat")
            self._terminal_topic = self._topic(topics, terminal)
        except ValueError as exc:
            raise CaptureWatchPolicyError(str(exc)) from exc
        self._publisher = event_publisher
        self._event_bus = event_bus
        self._reader = reader
        self._clock = clock or time.time
        self._host = host or socket.gethostname()
        self._pid = pid if pid is not None else os.getpid()

    @staticmethod
    def _topic(topics: tuple[str, ...], fragment: str) -> str:
        matches = [topic for topic in topics if fragment in topic]
        if len(matches) != 1:
            raise CaptureWatchPolicyError(f"contract must declare one {fragment} topic")
        return matches[0]

    async def handle(
        self, request: ModelCaptureWatchTrigger
    ) -> ModelCaptureWatchEvaluation:
        """Return every finding and delivery attempt; emit progress on every tick."""
        epoch = self._clock()
        now = datetime.fromtimestamp(epoch, tz=UTC)
        active_since = now - timedelta(seconds=self._policy.active_window_seconds)
        correlation_id = request.correlation_id or uuid4()
        verdicts: list[ModelCaptureWatchVerdict] = []
        deliveries: list[ModelCaptureWatchDelivery] = []
        for source in self._sources:
            error: str | None = None
            observation: ModelCaptureObservation | None = None
            try:
                if self._reader is None:
                    # Resolve at evaluation so a missing DSN becomes UNKNOWN,
                    # and a later tick can recover once the binding is available.
                    self._reader = PostgresCaptureWatchReader(
                        dsn=resolve_reads_source_dsn(self._binding)
                    )
                observation = ModelCaptureObservation.model_validate(
                    await asyncio.to_thread(
                        self._reader.read, source, active_since=active_since
                    )
                )
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
            findings = classify_capture_silence(
                source=source, observation=observation, now=now, error=error
            )
            verdicts.extend(findings)
            source_verdict = findings[0]
            if source_verdict.state is EnumCaptureWatchState.UNKNOWN:
                logger.error(
                    "Capture UNKNOWN: source_id=%s error=%s",
                    source.source_id,
                    source_verdict.error,
                )
            elif source_verdict.state is EnumCaptureWatchState.STALE:
                deliveries.append(
                    await self._publish_alarm(
                        source, source_verdict, epoch, correlation_id
                    )
                )
            else:
                for verdict in findings[1:]:
                    if verdict.state is EnumCaptureWatchState.SILENT:
                        deliveries.append(
                            await self._publish_alarm(
                                source, verdict, epoch, correlation_id
                            )
                        )
        heartbeat = build_heartbeat(
            process_id=self._policy.heartbeat_process_id,
            host=self._host,
            pid=self._pid,
            process_started_at=now,
            now=now,
            progress_counter=int(epoch) // _SECONDS_PER_MINUTE,
            demand_count=sum(verdict.scope == "actor" for verdict in verdicts),
        )
        heartbeat_error = await self._publish_json(
            self._heartbeat_topic,
            heartbeat.model_dump_json(exclude_none=True).encode("utf-8"),
        )
        evaluation = ModelCaptureWatchEvaluation(
            evaluated_at=now,
            verdicts=tuple(verdicts),
            deliveries=tuple(deliveries),
            heartbeat_published=heartbeat_error is None,
            heartbeat_error=heartbeat_error,
        )
        await self._publish_json(
            self._terminal_topic, evaluation.model_dump_json().encode("utf-8")
        )
        return evaluation

    async def _publish_json(self, topic: str, payload: bytes) -> str | None:
        try:
            if self._publisher is not None:
                pending = self._publisher(topic, payload)
                if inspect.isawaitable(pending):
                    await pending
            elif self._event_bus is not None:
                await self._event_bus.publish(topic, None, payload)
            else:
                raise RuntimeError(
                    "no event_publisher or event_bus was injected; event was not published"
                )
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            logger.error("Capture event NOT published: topic=%s error=%s", topic, error)
            return error
        return None

    async def _publish_alarm(
        self,
        source: ModelCaptureSource,
        verdict: ModelCaptureWatchVerdict,
        epoch: float,
        correlation_id: UUID,
    ) -> ModelCaptureWatchDelivery:
        actor = verdict.actor if verdict.scope == "actor" else "*"
        newest = (
            str(int(verdict.newest_at.timestamp()))
            if verdict.newest_at is not None
            else "none"
        )
        key = (
            f"capture-silence-watch|{source.source_id}|{actor}|{verdict.state}|{newest}"
        )
        if verdict.newest_at is None:
            key += f"|{int(epoch) // self._policy.renotify_after_seconds}"
        error: str | None = None
        try:
            ref = contract_secret_ref(self._contract_path, _CHANNEL_SECRET)
            secret = resolve_api_key_loop_safe(ref, env_var_fallback=ref)
            if secret is None:
                raise RuntimeError(f"secret ref {ref!r} resolved to None")
            command = build_slack_command(
                source=source,
                policy=self._policy,
                verdict=verdict,
                channel=secret.get_secret_value(),
                idempotency_key=key,
                correlation_id=correlation_id,
            )
            error = await self._publish_json(
                self._slack_topic, command.model_dump_json().encode("utf-8")
            )
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        if error is not None:
            logger.error(
                "Capture alarm NOT published: source_id=%s actor=%s error=%s",
                source.source_id,
                verdict.actor,
                error,
            )
        return ModelCaptureWatchDelivery(
            source_id=source.source_id,
            actor=verdict.actor,
            command_topic=self._slack_topic,
            idempotency_key=key,
            published=error is None,
            error=error,
        )
