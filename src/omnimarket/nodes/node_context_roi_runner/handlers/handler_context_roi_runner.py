# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerContextRoiRunner -- context-ROI experiment EFFECT.

Drives the generation pipeline over the bus per (task x factor_subset x trial).
No in-process generation handler import; no direct Kafka runner use; no
hardcoded topic literals.  Topics are read from contract.yaml at construction
time.

Flow per (task x arm x trial):
  1. Assemble context pack text for the arm (off arm = empty string).
     Context text is assembled directly from EnumContextFactor labels and
     stub content -- no cross-node import of pack builder private models.
     In production the content resolver populates real artifact text via
     the request's artifact_content_map before calling this handler.
  2. Serialise pack text; compute SHA-256 hash.
  3. Publish a generation command on the generation command topic
     (read from contract via generation_pipeline.command_topic).
  4. Wait for the terminal generation event on the generation terminal event
     topic (read from contract via generation_pipeline.terminal_event_topic),
     correlating by correlation_id.
  5. Read back attempt_count / first_pass_success / prompt+completion tokens /
     provider / model_id from the typed event payload.  Fail closed if a
     required event field is absent (no silent default that fakes a result).
  6. Emit a ModelAttemptReductionRow.

Statistical validity:
  - K trials per cell (from request.trials_per_cell).
  - Arm order randomised within each task using request.arm_order_seed.
  - run_order recorded on every row.
  - Rows are proof_class=RUNTIME_OBSERVED_ONLY; freeze as fixtures for
    REPLAY_PROVEN scoring downstream.

Architecture conformance:
  - EFFECT: contract + handler, all I/O here.
  - No cross-node model imports -- all shared types come from omnibase_core
    or this node's own models package.
  - Bus publish/consume is the only cross-service I/O channel.
  - Model/provider/endpoint identity comes from the routing authority
    (contract model_routing fields on the generation contract), echoed back
    on event result fields -- never hardcoded in this handler.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import yaml
from omnibase_core.enums.enum_context_factor import EnumContextFactor

from omnimarket.enums.enum_proof_class import EnumProofClass
from omnimarket.events.context_roi import (
    EnumFailureStage,
    ModelAttemptReductionRow,
    ModelContextRoiRunResult,
)
from omnimarket.nodes.node_context_roi_runner.models.model_context_roi_run_request import (
    ModelContextRoiArmSpec,
    ModelContextRoiRunRequest,
    ModelContextRoiTask,
)

logger = logging.getLogger(__name__)

# Path to contract.yaml for this node.
# Topics are resolved from this file -- never hardcoded in handler logic.
_RUNNER_CONTRACT_PATH = Path(__file__).parent.parent / "contract.yaml"

# Type alias for the injectable event publisher (topic, payload) -> None.
EventPublisher = Callable[[str, bytes], None]

# Type alias for the injectable event consumer:
# (topic, correlation_id, timeout_seconds) -> dict[str, Any] | None
# Returns the raw deserialized event payload, or None on timeout/failure.
EventConsumer = Callable[[str, str, float], dict[str, Any] | None]


@runtime_checkable
class TerminalConsumerSessionLike(Protocol):
    """Two-phase session over a single terminal topic (OMN-13012).

    Produced by ``TwoPhaseEventConsumer.open(topic)`` already positioned (assign +
    seek_to_end done at ``open`` time, BEFORE the caller publishes). ``wait`` then
    blocks from that captured position, so a terminal emitted in the publish→wait
    gap is still delivered — closing the subscribe-after-publish race that made
    every row degenerate (probe3).
    """

    def wait(
        self, correlation_id: str, timeout_seconds: float
    ) -> dict[str, Any] | None: ...

    def close(self) -> None: ...


@runtime_checkable
class TwoPhaseEventConsumer(Protocol):
    """Injected ``event_consumer`` that supports subscribe-before-publish.

    Directly callable with the legacy single-call shape AND exposes
    ``open(topic) -> TerminalConsumerSessionLike`` so the runner can position the
    terminal consumer to current end BEFORE publishing each generation command.
    The runtime injects ``omnibase_infra``'s
    ``service_terminal_event_consumer.TerminalEventConsumer`` here.
    """

    def open(self, terminal_topic: str) -> TerminalConsumerSessionLike: ...

    def __call__(
        self, terminal_topic: str, correlation_id: str, timeout_seconds: float
    ) -> dict[str, Any] | None: ...


def _noop_publisher(topic: str, payload: bytes) -> None:
    logger.debug("[roi-runner] noop publish to %s (%d bytes)", topic, len(payload))


def _noop_consumer(
    topic: str, correlation_id: str, timeout_seconds: float
) -> dict[str, Any] | None:
    logger.debug("[roi-runner] noop consume from %s for %s", topic, correlation_id)
    return None


def _load_yaml(path: Path) -> dict[str, Any]:
    with open(path) as f:
        data: dict[str, Any] = yaml.safe_load(f)
    return data


def _sha256(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _utc_now_iso() -> str:
    return datetime.now(tz=UTC).isoformat()


def _factor_str_to_enum(label: str) -> EnumContextFactor | None:
    """Convert a factor label string to EnumContextFactor, or None if unknown."""
    for member in EnumContextFactor:
        if member.value == label:
            return member
    return None


def _assemble_context_text(
    factor_subset: tuple[str, ...],
    artifact_content_map: dict[str, str],
) -> tuple[str, list[str]]:
    """Assemble context pack text from factor labels and resolved content.

    Returns (context_text, warnings).  Unknown factor labels produce a
    warning and are skipped.  In production the artifact_content_map is
    populated by the content-resolver effect before this handler is called.
    When the map is empty (offline test runs), each factor falls back to a
    minimal synthetic section so the assembly path stays exercisable.

    Context text format: one labelled section per factor, separated by
    blank lines -- the same shape the generation prompt expects.
    """
    warnings: list[str] = []
    parts: list[str] = []

    for label in factor_subset:
        factor = _factor_str_to_enum(label)
        if factor is None:
            warnings.append(f"unknown factor label '{label}' -- skipped")
            continue
        content = artifact_content_map.get(label, f"[stub content for {label}]")
        parts.append(f"[{label}]\n{content}")

    return "\n\n".join(parts), warnings


class HandlerContextRoiRunner:
    """EFFECT handler: drive generation over the bus per (task x arm x trial).

    The event_publisher and event_consumer are injected for testing.  In
    production the runtime injects the Kafka adapter implementations.

    Architecture note: context text is assembled directly from factor labels
    and resolved artifact content -- no cross-node import of pack builder
    private models.  The pack builder contract is the source of truth for
    factor ordering and budget policy; this handler implements a simplified
    assembly path suitable for experiment runs.
    """

    def __init__(
        self,
        event_publisher: EventPublisher | None = None,
        event_consumer: EventConsumer | None = None,
        runner_contract_path: Path | None = None,
    ) -> None:
        self._publisher: EventPublisher = event_publisher or _noop_publisher
        self._consumer: EventConsumer = event_consumer or _noop_consumer

        runner_contract = _load_yaml(runner_contract_path or _RUNNER_CONTRACT_PATH)
        publish_topics: list[str] = runner_contract.get("event_bus", {}).get(
            "publish_topics", []
        )
        self._topic_completed = next(
            (t for t in publish_topics if "context-roi-run-completed" in t), ""
        )
        self._topic_failed = next(
            (t for t in publish_topics if "context-roi-run-failed" in t), ""
        )

        # Generation pipeline topics -- read from runner contract's
        # generation_pipeline section (both already declared on the generation
        # consumer contract on both ends).
        gen_pipeline = runner_contract.get("generation_pipeline", {})
        self._gen_command_topic: str = gen_pipeline.get("command_topic", "")
        self._gen_terminal_topic: str = gen_pipeline.get("terminal_event_topic", "")

        if not self._gen_command_topic or not self._gen_terminal_topic:
            raise ValueError(
                "contract.yaml generation_pipeline.command_topic and "
                "generation_pipeline.terminal_event_topic are required; "
                "do not hardcode topic strings in the handler"
            )

    # ------------------------------------------------------------------
    # Public handle entry point
    # ------------------------------------------------------------------

    def handle(self, request: ModelContextRoiRunRequest) -> ModelContextRoiRunResult:
        """Run the full experiment matrix and return all captured rows."""
        rows: list[ModelAttemptReductionRow] = []
        warnings: list[str] = []
        global_order = 0

        for task in request.tasks:
            # Randomise arm order within each task for order-effect control.
            # Derive the per-task offset from a stable SHA-256 of the task id so
            # the same arm_order_seed reproduces the same arm order across
            # process runs (Python's builtin hash() is salted per process).
            task_seed = int(
                hashlib.sha256(task.task_id.encode("utf-8")).hexdigest()[:8], 16
            )
            rng = random.Random(request.arm_order_seed + task_seed)
            arm_order = list(request.arms)
            rng.shuffle(arm_order)

            for trial_num in range(1, request.trials_per_cell + 1):
                for arm in arm_order:
                    global_order += 1
                    row, trial_warnings = self._run_trial(
                        task=task,
                        arm=arm,
                        trial_num=trial_num,
                        run_order=global_order,
                        request=request,
                    )
                    rows.append(row)
                    warnings.extend(trial_warnings)

        failed_count = sum(1 for r in rows if r.failure_stage != EnumFailureStage.NONE)

        result = ModelContextRoiRunResult(
            run_id=request.run_id,
            rows=tuple(rows),
            proof_class=EnumProofClass.RUNTIME_OBSERVED_ONLY,
            total_trials=len(rows),
            failed_trials=failed_count,
            warnings=tuple(warnings),
        )
        self._emit_result(result)
        return result

    async def handle_async(
        self, request: ModelContextRoiRunRequest
    ) -> ModelContextRoiRunResult:
        """Runtime dispatch entry point — runs the blocking ``handle`` off the loop.

        The runtime auto-wiring dispatch callback prefers ``handle_async`` when a
        handler declares it (omnibase_infra ``handler_wiring._make_dispatch_callback``).
        ``handle`` is synchronous and blocks up to ``generation_timeout_seconds``
        per arm on the correlated terminal-event consumer. If it ran directly on
        the single effects-container event loop it would pin that loop, starving
        every co-resident consumer group's poll/heartbeat — including
        ``node_generation_consumer``, the consumer that must produce the terminal
        this runner awaits. That starvation (broker-verified mass
        ``UnknownMemberIdError`` rebalance, OMN-13010) made the generation
        terminals arrive only after the runner's windows had already closed, so
        every row was degenerate.

        Offloading the blocking ``handle`` to a worker thread via
        ``asyncio.to_thread`` keeps the dispatch loop free for the full duration
        of the runner's blocking waits, so generation runs concurrently and its
        terminal arrives inside the window. ``handle`` stays the synchronous
        standalone/test entry point.
        """
        return await asyncio.to_thread(self.handle, request)

    # ------------------------------------------------------------------
    # Per-trial logic
    # ------------------------------------------------------------------

    def _open_terminal_session(self) -> TerminalConsumerSessionLike | None:
        """Open a positioned terminal session BEFORE publishing, if supported.

        Returns a two-phase session (assign + seek_to_end already done at current
        end) when the injected consumer exposes ``open(topic)`` — the runtime's
        ``TerminalEventConsumer`` (OMN-13012). Returns ``None`` for the legacy
        single-call consumer or the no-op default, in which case the caller falls
        back to seek-at-wait (the original, race-prone path).
        """
        consumer = self._consumer
        opener = getattr(consumer, "open", None)
        if not callable(opener):
            return None
        try:
            session = opener(self._gen_terminal_topic)
        except Exception as exc:
            logger.warning(
                "[roi-runner] failed to open terminal session on %s; "
                "falling back to single-call consume: %s",
                self._gen_terminal_topic,
                exc,
            )
            return None
        if isinstance(session, TerminalConsumerSessionLike):
            return session
        logger.warning(
            "[roi-runner] consumer.open(%s) returned a non-session object %r; "
            "falling back to single-call consume",
            self._gen_terminal_topic,
            type(session).__name__,
        )
        close = getattr(session, "close", None)
        if callable(close):
            close()
        return None

    def _run_trial(
        self,
        task: ModelContextRoiTask,
        arm: ModelContextRoiArmSpec,
        trial_num: int,
        run_order: int,
        request: ModelContextRoiRunRequest,
    ) -> tuple[ModelAttemptReductionRow, list[str]]:
        """Execute one (task x arm x trial) and return a row + any warnings."""
        warnings: list[str] = []
        correlation_id = str(uuid.uuid4())

        # --- Step 1: assemble context pack text (off arm = empty string) ---
        context_pack_text = ""
        context_pack_hash = ""
        pack_failure_stage = EnumFailureStage.NONE

        if arm.factor_subset:
            # Enforce the task's required-factor contract for ON arms: a
            # required factor absent from the arm subset is a pack_build
            # failure (the request model declares this pack-build semantics).
            missing_required = [
                label
                for label in task.required_factors
                if label not in arm.factor_subset
            ]
            if missing_required:
                warnings.append(
                    f"[{arm.label}] missing required factor(s) "
                    f"{missing_required} for task {task.task_id} -- pack_build failure"
                )
                pack_failure_stage = EnumFailureStage.PACK_BUILD

            if pack_failure_stage == EnumFailureStage.NONE:
                context_pack_text, assemble_warnings = _assemble_context_text(
                    factor_subset=arm.factor_subset,
                    artifact_content_map=request.artifact_content_map,
                )
                warnings.extend(assemble_warnings)

                # If every factor label was unknown, record pack_build failure.
                valid_factors = [
                    label
                    for label in arm.factor_subset
                    if _factor_str_to_enum(label) is not None
                ]
                if not valid_factors:
                    pack_failure_stage = EnumFailureStage.PACK_BUILD

                if context_pack_text:
                    context_pack_hash = _sha256(context_pack_text)

        if pack_failure_stage != EnumFailureStage.NONE:
            # Pack assembly failed -- record row without attempting generation.
            return (
                ModelAttemptReductionRow(
                    run_id=request.run_id,
                    correlation_id=correlation_id,
                    task_id=task.task_id,
                    run_order=run_order,
                    context_factor_subset=arm.label,
                    context_pack_hash=context_pack_hash,
                    failure_stage=pack_failure_stage,
                    proof_class=EnumProofClass.RUNTIME_OBSERVED_ONLY,
                ),
                warnings,
            )

        # --- Step 2a: subscribe-before-publish (OMN-13012) ---
        # Position the terminal consumer to the current end BEFORE publishing the
        # command. With the dispatch loop freed (OMN-13010) generation completes
        # in ~1s, so a single-call consumer that seeks AFTER publishing skips past
        # the already-emitted terminal (probe3). A two-phase session captures the
        # pre-publish offset here; the post-publish wait resumes from it. If the
        # injected consumer is the legacy single-call form (or the no-op default),
        # session stays None and we fall back to seek-at-wait below.
        session = self._open_terminal_session()

        # --- Step 2b: publish generation command over the bus ---
        command_payload = {
            "task_description": task.task_description,
            "correlation_id": correlation_id,
            "max_attempts": request.max_attempts,
            "context_pack": context_pack_text,
            "context_artifacts": [],
            "context_pack_hash": context_pack_hash,
        }
        try:
            self._publisher(
                self._gen_command_topic,
                json.dumps(command_payload).encode("utf-8"),
            )
        except Exception as exc:
            logger.warning(
                "[roi-runner] failed to publish generation command "
                "(task=%s arm=%s trial=%d): %s",
                task.task_id,
                arm.label,
                trial_num,
                exc,
            )
            if session is not None:
                session.close()
            return (
                ModelAttemptReductionRow(
                    run_id=request.run_id,
                    correlation_id=correlation_id,
                    task_id=task.task_id,
                    run_order=run_order,
                    context_factor_subset=arm.label,
                    context_pack_hash=context_pack_hash,
                    failure_stage=EnumFailureStage.GENERATION,
                    proof_class=EnumProofClass.RUNTIME_OBSERVED_ONLY,
                ),
                warnings,
            )

        # --- Step 3: consume terminal generation event (block AFTER publish) ---
        if session is not None:
            event_payload = session.wait(
                correlation_id, request.generation_timeout_seconds
            )
        else:
            event_payload = self._consumer(
                self._gen_terminal_topic,
                correlation_id,
                request.generation_timeout_seconds,
            )

        if event_payload is None:
            logger.warning(
                "[roi-runner] no terminal event received "
                "(task=%s arm=%s trial=%d correlation_id=%s)",
                task.task_id,
                arm.label,
                trial_num,
                correlation_id,
            )
            return (
                ModelAttemptReductionRow(
                    run_id=request.run_id,
                    correlation_id=correlation_id,
                    task_id=task.task_id,
                    run_order=run_order,
                    context_factor_subset=arm.label,
                    context_pack_hash=context_pack_hash,
                    failure_stage=EnumFailureStage.GENERATION,
                    proof_class=EnumProofClass.RUNTIME_OBSERVED_ONLY,
                ),
                warnings,
            )

        # --- Step 4: extract typed fields from event; fail closed on missing ---
        row, extract_warnings = self._extract_row(
            event_payload=event_payload,
            run_id=request.run_id,
            correlation_id=correlation_id,
            task_id=task.task_id,
            arm_label=arm.label,
            context_pack_hash=context_pack_hash,
            run_order=run_order,
        )
        warnings.extend(extract_warnings)
        return row, warnings

    # ------------------------------------------------------------------
    # Row extraction from terminal event payload
    # ------------------------------------------------------------------

    def _extract_row(
        self,
        event_payload: dict[str, Any],
        run_id: str,
        correlation_id: str,
        task_id: str,
        arm_label: str,
        context_pack_hash: str,
        run_order: int,
    ) -> tuple[ModelAttemptReductionRow, list[str]]:
        """Extract a ModelAttemptReductionRow from the terminal event dict.

        Fails closed: if attempt_count is absent the row records
        failure_stage=GENERATION rather than silently faking a result.
        """
        warnings: list[str] = []

        # attempt_count is required -- absence OR a non-numeric value means the
        # event is malformed. Fail this row closed rather than letting an
        # uncaught int()/float() cast abort the whole run's remaining trials.
        def _fail_closed_row(reason: str) -> tuple[ModelAttemptReductionRow, list[str]]:
            warnings.append(
                f"[{arm_label}] {reason} -- recording as generation failure"
            )
            return (
                ModelAttemptReductionRow(
                    run_id=run_id,
                    correlation_id=correlation_id,
                    task_id=task_id,
                    run_order=run_order,
                    context_factor_subset=arm_label,
                    context_pack_hash=context_pack_hash,
                    failure_stage=EnumFailureStage.GENERATION,
                    proof_class=EnumProofClass.RUNTIME_OBSERVED_ONLY,
                ),
                warnings,
            )

        if "attempt_count" not in event_payload:
            return _fail_closed_row(
                "terminal event missing 'attempt_count' (fail-closed)"
            )

        try:
            attempt_count = int(event_payload["attempt_count"])
            prompt_tokens = int(event_payload.get("prompt_tokens", 0))
            completion_tokens = int(event_payload.get("completion_tokens", 0))
            cost_usd = float(event_payload.get("cost_inference_usd", 0.0))
        except (TypeError, ValueError) as exc:
            return _fail_closed_row(
                f"terminal event has non-numeric telemetry field ({exc})"
            )

        contract_passed: bool = bool(event_payload.get("contract_passed", False))
        first_pass_success: bool = bool(event_payload.get("first_pass_success", False))
        model_id: str = str(event_payload.get("model_id", ""))
        provider: str = str(event_payload.get("provider", ""))
        endpoint_class: str = str(event_payload.get("endpoint_class", ""))

        # Warn (but do not fail closed) on absent P2-1 optional fields.
        if not model_id:
            warnings.append(
                f"[{arm_label}] terminal event missing 'model_id' -- "
                "row recorded with empty model_id"
            )
        if not provider:
            warnings.append(
                f"[{arm_label}] terminal event missing 'provider' -- "
                "row recorded with empty provider"
            )

        failure_stage = (
            EnumFailureStage.NONE if contract_passed else EnumFailureStage.VALIDATION
        )

        return (
            ModelAttemptReductionRow(
                run_id=run_id,
                correlation_id=correlation_id,
                task_id=task_id,
                run_order=run_order,
                context_factor_subset=arm_label,
                context_pack_hash=context_pack_hash,
                attempt_count=attempt_count,
                first_pass_success=first_pass_success,
                final_success=contract_passed,
                failure_stage=failure_stage,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                estimated_cost=cost_usd,
                model_id=model_id,
                provider=provider,
                endpoint_ref=endpoint_class,
                proof_class=EnumProofClass.RUNTIME_OBSERVED_ONLY,
            ),
            warnings,
        )

    # ------------------------------------------------------------------
    # Emit result event
    # ------------------------------------------------------------------

    def _emit_result(self, result: ModelContextRoiRunResult) -> None:
        topic = self._topic_completed
        if not topic:
            logger.warning(
                "[roi-runner] no completed topic configured; result not emitted"
            )
            return
        try:
            payload = json.dumps(result.model_dump()).encode("utf-8")
            self._publisher(topic, payload)
        except Exception as exc:
            logger.warning("[roi-runner] emit result to %s failed: %s", topic, exc)


__all__ = [
    "HandlerContextRoiRunner",
    "_assemble_context_text",
    "_factor_str_to_enum",
    "_sha256",
]
