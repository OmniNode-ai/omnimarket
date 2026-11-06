# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The serve host of the dev-merge redeploy trigger (OMN-22478, plan section 8 step R2).

One serve process on a lab host reads ``onex.evt.omnimarket.repo-merged.v1`` in a consumer
group shared by every serving instance (each merge is handled by exactly one), publishes the
redeploy-start command for a merge that reaches the lane, reads the redeploy orchestrator's
terminal event in a group of its own (the instance holding the pending merge is the one that
acts), then reads the lane back and publishes one receipt. Every merge ends in exactly one
receipt: a decline, a typed failure, a verified lane, a lane that is behind, or a missed
deadline. Silence is not an outcome.

Topics come only from the node's ``contract.yaml``; policy only from its overlay.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from importlib import resources
from uuid import UUID

import yaml
from omnibase_core.event_bus.util_consumer_group import derive_service_group_id
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from pydantic import ValidationError

from omnimarket.delegated_test_loop.lab_run_bus import (
    ProtocolBusMessage,
    ProtocolLabRunBus,
    event_type_for,
)
from omnimarket.events.runtime_deployment import ModelRedeployCompletedEvent
from omnimarket.lab_work.bus import _bytes, _subscribe
from omnimarket.models.dev_merge_redeploy_overlay import (
    EnumDevMergeMode,
    ModelDevMergeRedeployPolicy,
)
from omnimarket.models.model_repo_merged import ModelRepoMerged
from omnimarket.nodes.node_dev_merge_redeploy_trigger_effect.handlers.handler_dev_merge_redeploy_trigger import (
    HandlerDevMergeRedeployTrigger,
    correlation_id_for,
)
from omnimarket.nodes.node_dev_merge_redeploy_trigger_effect.handlers.handler_dev_merge_redeploy_verify import (
    OBSERVED_PHASE,
    HandlerDevMergeRedeployVerify,
)
from omnimarket.nodes.node_dev_merge_redeploy_trigger_effect.models.model_dev_merge_decision import (
    ModelDevMergeDecision,
    ModelDevMergePending,
    ModelDevMergeVerdict,
)
from omnimarket.nodes.node_dev_merge_redeploy_trigger_effect.models.model_dev_merge_redeploy import (
    EnumDevMergeOutcome,
    ModelDevMergeRedeployReceipt,
    ModelDevMergeRedeployTopics,
)

logger = logging.getLogger(__name__)
NODE = "node_dev_merge_redeploy_trigger_effect"
GROUP_SERVICE = "omnimarket"


def load_dev_merge_redeploy_topics() -> ModelDevMergeRedeployTopics:
    text = (
        resources.files(f"omnimarket.nodes.{NODE}")
        .joinpath("contract.yaml")
        .read_text()
    )
    dispatch = yaml.safe_load(text)["runtime_dispatch"]
    return ModelDevMergeRedeployTopics(
        merged=dispatch["subscribe_topic"],
        start_command=dispatch["start_command_topic"],
        completed=dispatch["completed_topic"],
        receipt=dispatch["terminal_events"]["success"],
    )


def merged_group_id() -> str:
    """One group for every serving instance: each merge is handled once."""
    return derive_service_group_id(NODE, service=GROUP_SERVICE)


def completed_group_id(host_name: str) -> str:
    """One group per host: the host holding the pending merge sees the terminal event."""
    return f"{merged_group_id()}.completed.{host_name}"


def _payload(message: ProtocolBusMessage) -> object:
    raw = json.loads(message.value)
    return raw.get("payload", raw) if isinstance(raw, dict) else None


class DevMergeRedeployHost:
    """Trigger, track and verify the dev lane's redeploy for each configured dev merge."""

    def __init__(
        self,
        bus: ProtocolLabRunBus,
        trigger: HandlerDevMergeRedeployTrigger,
        verify: HandlerDevMergeRedeployVerify,
        policy: ModelDevMergeRedeployPolicy,
        *,
        host_name: str,
        deploy_bus: ProtocolLabRunBus | None = None,
        topics: ModelDevMergeRedeployTopics | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        # ``bus`` carries the merge events and the receipts. The start command and the terminal
        # event travel on the deploy lane's bus, which is the same one unless the deployment
        # keeps them on another broker.
        self._bus = bus
        self._deploy_bus = deploy_bus if deploy_bus is not None else bus
        self._trigger = trigger
        self._verify = verify
        self._policy = policy
        self._host = host_name
        self._topics = topics or load_dev_merge_redeploy_topics()
        self._clock = clock
        self._pending: dict[UUID, ModelDevMergePending] = {}
        self._deadlines: dict[UUID, asyncio.Task[None]] = {}
        self._seen: set[UUID] = set()
        self._tasks: set[asyncio.Task[None]] = set()
        self._unsubscribes: list[Callable[[], Awaitable[None]]] = []
        self.skipped = 0
        self.duplicates = 0
        self.unmatched_completions = 0

    @property
    def topics(self) -> ModelDevMergeRedeployTopics:
        return self._topics

    @property
    def pending(self) -> int:
        return len(self._pending)

    async def start(self) -> None:
        if self._unsubscribes:
            return
        self._unsubscribes = [
            await _subscribe(
                self._bus,
                self._topics.merged,
                self._on_merged,
                merged_group_id(),
                "latest",
            ),
        ]
        if self._policy.mode is EnumDevMergeMode.PUBLISH:
            self._unsubscribes.append(
                await _subscribe(
                    self._deploy_bus,
                    self._topics.completed,
                    self._on_completed,
                    completed_group_id(self._host),
                    "latest",
                )
            )

    async def stop(self) -> None:
        for unsubscribe in self._unsubscribes:
            await unsubscribe()
        self._unsubscribes = []
        for task in [*self._deadlines.values(), *self._tasks]:
            task.cancel()
        for task in [*self._deadlines.values(), *self._tasks]:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._deadlines.clear()
        self._tasks.clear()

    async def drain(self) -> None:
        while self._tasks or self._deadlines:
            await asyncio.gather(
                *self._tasks, *self._deadlines.values(), return_exceptions=True
            )

    def _skip(self, why: str) -> None:
        self.skipped += 1
        logger.info("dev-merge-redeploy %s skipped: %s", self._host, why)

    async def _on_merged(self, message: ProtocolBusMessage) -> None:
        try:
            event = ModelRepoMerged.model_validate(_payload(message))
        except (UnicodeDecodeError, json.JSONDecodeError, ValidationError) as exc:
            self._skip(f"not a repo-merged event ({type(exc).__name__})")
            return
        if event.state != "merged":
            self._skip(f"{event.repo}#{event.pr_number} is {event.state}")
            return
        cid = correlation_id_for(event.repo, event.merge_sha)
        if cid in self._seen:
            self.duplicates += 1
            logger.info("dev-merge-redeploy %s: %s already handled", self._host, cid)
            return
        self._seen.add(cid)
        task = asyncio.create_task(self._handle_merge(event))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _handle_merge(self, event: ModelRepoMerged) -> None:
        cid = correlation_id_for(event.repo, event.merge_sha)
        try:
            decision = await asyncio.to_thread(self._trigger.handle, event)
        except Exception as exc:
            logger.exception(
                "dev-merge-redeploy %s: trigger raised for %s", self._host, cid
            )
            decision = ModelDevMergeDecision(
                correlation_id=cid,
                outcome=EnumDevMergeOutcome.FAILED_INTERNAL,
                message=f"{type(exc).__name__}: {exc}"[-2000:],
            )
        if decision.command is None:
            await self._receipt(event, decision, published=False)
            return
        pending = ModelDevMergePending(event=event, decision=decision)
        if self._policy.mode is EnumDevMergeMode.OBSERVE:
            task = asyncio.create_task(self._observe(pending))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            return
        self._pending[cid] = pending
        try:
            envelope = ModelEventEnvelope[dict[str, object]](
                payload=decision.command.model_dump(mode="json"),
                event_type=event_type_for(self._topics.start_command),
                correlation_id=cid,
            )
            await self._deploy_bus.publish(
                self._topics.start_command, str(cid).encode("utf-8"), _bytes(envelope)
            )
        except (OSError, RuntimeError, TimeoutError) as exc:
            self._pending.pop(cid, None)
            failed = decision.model_copy(
                update={
                    "outcome": EnumDevMergeOutcome.FAILED_PUBLISH,
                    "command": None,
                    "message": f"{type(exc).__name__}: {exc}"[-2000:],
                }
            )
            await self._receipt(event, failed, published=False)
            return
        self._deadlines[cid] = asyncio.create_task(self._expire(cid))

    async def _observe(self, pending: ModelDevMergePending) -> None:
        """Read the lane until it carries the merge or the deadline passes; publish no command."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._policy.verify_deadline_seconds
        last = ModelDevMergeVerdict(
            outcome=EnumDevMergeOutcome.LANE_UNREADABLE,
            final_phase=OBSERVED_PHASE,
            message="no lane read finished",
        )
        while True:
            try:
                verdict = await asyncio.to_thread(self._verify.observe, pending)
            except Exception as exc:
                logger.exception("dev-merge-redeploy %s: observe raised", self._host)
                verdict = ModelDevMergeVerdict(
                    outcome=EnumDevMergeOutcome.FAILED_INTERNAL,
                    final_phase=OBSERVED_PHASE,
                    message=f"{type(exc).__name__}: {exc}"[-2000:],
                )
            if verdict.outcome in (
                EnumDevMergeOutcome.VERIFIED,
                EnumDevMergeOutcome.FAILED_INTERNAL,
            ):
                await self._receipt(
                    pending.event, pending.decision, published=False, verdict=verdict
                )
                return
            last = verdict
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            await asyncio.sleep(min(self._policy.observe_poll_seconds, remaining))
        missed = ModelDevMergeVerdict(
            outcome=EnumDevMergeOutcome.DEADLINE_MISSED,
            final_phase=OBSERVED_PHASE,
            lane_revision=last.lane_revision,
            message=(
                f"the lane did not carry the merge in {self._policy.verify_deadline_seconds:g}s; "
                f"last read: {last.message}"
            )[-2000:],
        )
        await self._receipt(
            pending.event, pending.decision, published=False, verdict=missed
        )

    async def _expire(self, cid: UUID) -> None:
        await asyncio.sleep(self._policy.verify_deadline_seconds)
        pending = self._pending.pop(cid, None)
        self._deadlines.pop(cid, None)
        if pending is None:
            return
        verdict = ModelDevMergeVerdict(
            outcome=EnumDevMergeOutcome.DEADLINE_MISSED,
            message=(
                f"no terminal redeploy event inside {self._policy.verify_deadline_seconds:g}s"
            ),
        )
        await self._receipt(
            pending.event, pending.decision, published=True, verdict=verdict
        )

    async def _on_completed(self, message: ProtocolBusMessage) -> None:
        try:
            completed = ModelRedeployCompletedEvent.model_validate(_payload(message))
        except (UnicodeDecodeError, json.JSONDecodeError, ValidationError) as exc:
            self._skip(f"not a redeploy-completed event ({type(exc).__name__})")
            return
        pending = self._pending.pop(completed.correlation_id, None)
        if pending is None:
            self.unmatched_completions += 1
            return
        deadline = self._deadlines.pop(completed.correlation_id, None)
        if deadline is not None:
            deadline.cancel()
        task = asyncio.create_task(self._verify_and_receipt(pending, completed))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _verify_and_receipt(
        self, pending: ModelDevMergePending, completed: ModelRedeployCompletedEvent
    ) -> None:
        try:
            verdict = await asyncio.to_thread(self._verify.handle, pending, completed)
        except Exception as exc:
            logger.exception("dev-merge-redeploy %s: verify raised", self._host)
            verdict = ModelDevMergeVerdict(
                outcome=EnumDevMergeOutcome.FAILED_INTERNAL,
                final_phase=str(completed.final_phase),
                message=f"{type(exc).__name__}: {exc}"[-2000:],
            )
        await self._receipt(
            pending.event, pending.decision, published=True, verdict=verdict
        )

    async def _receipt(
        self,
        event: ModelRepoMerged,
        decision: ModelDevMergeDecision,
        *,
        published: bool,
        verdict: ModelDevMergeVerdict | None = None,
    ) -> None:
        outcome = verdict.outcome if verdict else decision.outcome
        if outcome is None:
            raise RuntimeError("a receipt needs an outcome")
        receipt = ModelDevMergeRedeployReceipt(
            host=self._host,
            repo=event.repo,
            pr_number=event.pr_number,
            merge_sha=event.merge_sha,
            base=event.base,
            correlation_id=decision.correlation_id,
            mode=self._policy.mode,
            outcome=outcome,
            command_published=published,
            runtime_paths=decision.runtime_paths,
            final_phase=verdict.final_phase if verdict else "",
            lane_revision=verdict.lane_revision if verdict else "",
            message=(verdict.message if verdict else decision.message),
            observed_at=self._clock(),
        )
        envelope = ModelEventEnvelope[dict[str, object]](
            payload=receipt.model_dump(mode="json"),
            event_type=event_type_for(self._topics.receipt),
            correlation_id=receipt.correlation_id,
        )
        await self._bus.publish(
            self._topics.receipt,
            str(receipt.correlation_id).encode("utf-8"),
            _bytes(envelope),
        )
        logger.info(
            "dev-merge-redeploy %s %s %s#%d %s published=%s",
            self._host,
            receipt.mode,
            receipt.repo,
            receipt.pr_number,
            receipt.outcome,
            published,
        )


__all__ = [
    "DevMergeRedeployHost",
    "completed_group_id",
    "load_dev_merge_redeploy_topics",
    "merged_group_id",
]
