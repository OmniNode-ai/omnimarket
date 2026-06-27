# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerContextRoiRunner -- context-ROI experiment EFFECT.

Drives the generation pipeline over the bus per (task x factor_subset x trial).
No in-process generation handler import; no direct Kafka runner use; no
hardcoded topic literals.  Topics are read from contract.yaml at construction
time.

Flow per (task x arm x trial):
  1. Assemble the arm's context pack (off arm = empty string) by invoking the
     single canonical assembler, node_context_pack_builder_compute (OMN-13643).
     The builder owns factor precedence and the 16k token budget; the runner
     never re-implements them. In production the content resolver populates real
     artifact text via the request's artifact_content_map before this handler
     is called.
  2. Use the builder's pack_hash as the injected pack's context_pack_hash.
  3. Publish a generation command on the generation command topic
     (read from contract via generation_pipeline.command_topic).
  4. Wait for the terminal generation event, racing correlated waits across
     BOTH terminal topics -- completed (generation_pipeline.terminal_event_topic)
     AND failed (generation_pipeline.terminal_failed_event_topic) -- because the
     generation consumer routes contract_passed=False benchmarks to the failed
     topic (OMN-13038).  First correlated terminal wins; correlating by
     correlation_id.
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
  - Context-pack assembly is sourced from the single canonical assembler,
    node_context_pack_builder_compute, via its shared I/O models in
    omnimarket.pack (no cross-node reach-in into a sibling's private models).
  - Bus publish/consume is the only cross-service I/O channel.
  - Model/provider/endpoint identity comes from the routing authority
    (contract model_routing fields on the generation contract), echoed back
    on event result fields -- never hardcoded in this handler.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import logging
import random
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import yaml
from omnibase_core.enums.enum_context_factor import EnumContextFactor
from omnibase_core.enums.enum_context_pack_failure import EnumContextPackFailure
from omnibase_core.enums.enum_context_pack_provenance import (
    EnumContextPackProvenance,
)

from omnimarket.enums.enum_proof_class import EnumProofClass
from omnimarket.events.context_roi import (
    EnumFailureStage,
    ModelAttemptReductionRow,
    ModelContextRoiRunResult,
)
from omnimarket.nodes.node_context_pack_builder_compute.handlers.handler_context_pack_builder import (
    HandlerContextPackBuilder,
)
from omnimarket.nodes.node_context_roi_runner.models.model_context_roi_run_request import (
    ModelContextRoiArmSpec,
    ModelContextRoiRunRequest,
    ModelContextRoiTask,
)
from omnimarket.pack import (
    EnumContextPackBuilderStatus,
    ModelContextPackArtifact,
    ModelContextPackBuilderRequest,
    ModelContextProfile,
)

logger = logging.getLogger(__name__)

# Path to contract.yaml for this node.
# Topics are resolved from this file -- never hardcoded in handler logic.
_RUNNER_CONTRACT_PATH = Path(__file__).parent.parent / "contract.yaml"

# Backstop for the cross-topic terminal race (OMN-13038).  Each per-topic wait
# already bounds itself at timeout_seconds plus the session's own internal caps
# (assign cap 30s + submit grace 5s in omnibase_infra's TerminalConsumerSession),
# so this only guards against a wait that never returns at all.  Must exceed
# that internal 35s overhead.
_TERMINAL_RACE_GRACE_SECONDS = 45.0

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


def _factor_subset_hash(factor_subset: tuple[str, ...]) -> str:
    """Deterministic hash of an arm's factor-subset label set.

    Empty subset (the off arm) returns the empty string -- there is no factor
    combination to attribute.  A non-empty subset hashes the ordered labels so
    the same arm reproduces the same hash across runs, replay-auditing which
    factor combination produced the row (BAC plan line 111).
    """
    if not factor_subset:
        return ""
    canonical = json.dumps(list(factor_subset), separators=(",", ":"))
    return _sha256(canonical)


def _utc_now_iso() -> str:
    return datetime.now(tz=UTC).isoformat()


def _factor_str_to_enum(label: str) -> EnumContextFactor | None:
    """Convert a factor label string to EnumContextFactor, or None if unknown."""
    for member in EnumContextFactor:
        if member.value == label:
            return member
    return None


# heuristic_chars token estimation: ~4 chars per token, matching the pack
# builder profile default token_estimation_method="heuristic_chars". A non-empty
# body always estimates at least one token so a section is never silently dropped.
_CHARS_PER_TOKEN = 4

# Provenance label for the context-ROI pack profile. This is pack metadata
# (pack_id / pack_hash provenance), NOT model routing — the real model is
# resolved downstream by the generation consumer's routing authority. OMN-13644
# (B2) replaces this harness default with the ROI-resolved policy.
_PACK_PROFILE_MODEL_ID = "context-roi-experiment"


def _estimate_tokens(content: str) -> int:
    """heuristic_chars token estimate (≥1 for any non-empty content)."""
    return max(1, (len(content) + _CHARS_PER_TOKEN - 1) // _CHARS_PER_TOKEN)


def _map_builder_failure_stage(
    failure_class: EnumContextPackFailure | None,
) -> EnumFailureStage:
    if failure_class is EnumContextPackFailure.TOKEN_BUDGET_EXCEEDED:
        return EnumFailureStage.BUDGET_FAIL
    return EnumFailureStage.PACK_BUILD


def _build_context_pack(
    factor_subset: tuple[str, ...],
    artifact_content_map: dict[str, str],
    contract_hash: str,
) -> tuple[str, str, list[str], EnumFailureStage]:
    """Assemble the arm's context pack via the single canonical assembler.

    Sources the pack from node_context_pack_builder_compute (the one canonical
    context assembler) so the ROI runner never re-implements factor precedence
    or the 16k token budget. Returns
    ``(context_text, pack_hash, warnings, failure_stage)``:

    - ``context_text`` is one labelled ``[factor]\\ncontent`` section per chunk,
      in the builder's canonical precedence order
      (golden_chain > exemplar > local_failures > architecture_patterns >
      claude_md), joined by blank lines -- the shape the generation prompt
      expects.
    - ``pack_hash`` is the builder's ``pack_hash`` (NOT a local sha256 of the
      text), so the injected pack carries the canonical pack identity.
    - ``failure_stage`` is ``NONE`` on success; otherwise it preserves the
      builder's failure class so budget rejects remain ``BUDGET_FAIL``.

    Unknown factor labels are skipped with a warning. A selected factor without
    resolved content fails closed so upstream resolver gaps are not masked.
    """
    warnings: list[str] = []
    artifacts: list[ModelContextPackArtifact] = []

    for label in factor_subset:
        factor = _factor_str_to_enum(label)
        if factor is None:
            warnings.append(f"unknown factor label '{label}' -- skipped")
            continue
        if label not in artifact_content_map:
            warnings.append(
                f"missing resolved content for factor '{label}' -- pack build failed"
            )
            return "", "", warnings, EnumFailureStage.PACK_BUILD
        content = artifact_content_map[label]
        artifacts.append(
            ModelContextPackArtifact(
                factor=factor,
                content=content,
                token_estimate=_estimate_tokens(content),
                provenance=EnumContextPackProvenance.OBSERVED,
                source_artifact_hash=_sha256(content),
                source_contract_hash=contract_hash,
            )
        )

    if not artifacts:
        return "", "", warnings, EnumFailureStage.PACK_BUILD

    profile = ModelContextProfile(
        model_id=_PACK_PROFILE_MODEL_ID,
        factor_precedence=tuple(EnumContextFactor),
    )
    request = ModelContextPackBuilderRequest(
        contract_hash=contract_hash,
        generated_at=_utc_now_iso(),
        profile=profile,
        artifacts=tuple(artifacts),
    )
    result = HandlerContextPackBuilder().handle(request)

    if (
        result.status is not EnumContextPackBuilderStatus.OK
        or result.context_pack is None
        or result.pack_hash is None
    ):
        failure_stage = _map_builder_failure_stage(result.failure_class)
        warnings.append(
            f"context-pack builder rejected pack (failure_class="
            f"{failure_stage}): {'; '.join(result.errors)}"
        )
        return "", "", warnings, failure_stage

    context_text = "\n\n".join(
        f"[{chunk.factor.value}]\n{chunk.content}"
        for chunk in result.context_pack.chunks
    )
    return context_text, result.pack_hash, warnings, EnumFailureStage.NONE


class HandlerContextRoiRunner:
    """EFFECT handler: drive generation over the bus per (task x arm x trial).

    The event_publisher and event_consumer are injected for testing.  In
    production the runtime injects the Kafka adapter implementations.

    Architecture note: the context pack is sourced from the single canonical
    assembler, node_context_pack_builder_compute, invoked in-process via its
    handler (OMN-13643). Its shared I/O models live in ``omnimarket.pack`` (not
    the builder node's private models package), so this is not a cross-node
    reach-in. The builder owns factor precedence and the 16k token budget; this
    handler never re-implements that policy.
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
        # generation_pipeline section (all already declared on the generation
        # consumer contract on both ends).  The generation consumer routes
        # contract_passed=False benchmarks to the FAILED terminal topic, so the
        # runner must consume both terminals (OMN-13038).
        gen_pipeline = runner_contract.get("generation_pipeline", {})
        self._gen_command_topic: str = gen_pipeline.get("command_topic", "")
        self._gen_terminal_topic: str = gen_pipeline.get("terminal_event_topic", "")
        self._gen_terminal_failed_topic: str = gen_pipeline.get(
            "terminal_failed_event_topic", ""
        )

        if (
            not self._gen_command_topic
            or not self._gen_terminal_topic
            or not self._gen_terminal_failed_topic
        ):
            raise ValueError(
                "contract.yaml generation_pipeline.command_topic, "
                "generation_pipeline.terminal_event_topic and "
                "generation_pipeline.terminal_failed_event_topic are required; "
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

    def _open_terminal_session(
        self, terminal_topic: str
    ) -> TerminalConsumerSessionLike | None:
        """Open a positioned terminal session BEFORE publishing, if supported.

        Returns a two-phase session (assign + seek_to_end already done at current
        end) when the injected consumer exposes ``open(topic)`` — the runtime's
        ``TerminalEventConsumer`` (OMN-13012). Returns ``None`` for the legacy
        single-call consumer or the no-op default, in which case the caller falls
        back to seek-at-wait (the original, race-prone path) for that topic.
        """
        consumer = self._consumer
        opener = getattr(consumer, "open", None)
        if not callable(opener):
            return None
        try:
            session = opener(terminal_topic)
        except Exception as exc:
            logger.warning(
                "[roi-runner] failed to open terminal session on %s; "
                "falling back to single-call consume: %s",
                terminal_topic,
                exc,
            )
            return None
        if isinstance(session, TerminalConsumerSessionLike):
            return session
        logger.warning(
            "[roi-runner] consumer.open(%s) returned a non-session object %r; "
            "falling back to single-call consume",
            terminal_topic,
            type(session).__name__,
        )
        close = getattr(session, "close", None)
        if callable(close):
            close()
        return None

    def _close_terminal_sessions(
        self, sessions: dict[str, TerminalConsumerSessionLike | None]
    ) -> None:
        """Close every opened terminal session (idempotent, best-effort)."""
        for topic, session in sessions.items():
            if session is None:
                continue
            try:
                session.close()
            except Exception as exc:
                logger.warning(
                    "[roi-runner] failed to close terminal session on %s: %s",
                    topic,
                    exc,
                )

    def _await_first_terminal(
        self,
        sessions: dict[str, TerminalConsumerSessionLike | None],
        correlation_id: str,
        timeout_seconds: float,
    ) -> dict[str, Any] | None:
        """Race the correlated wait across all terminal topics (OMN-13038).

        The generation consumer emits exactly one terminal per command —
        completed (contract_passed=True) or failed (contract_passed=False) — on
        different topics, so the runner must wait on BOTH within the single
        per-arm timeout. The first correlated terminal wins; ``None`` only on
        genuine timeout (silence on every topic).

        Per topic: a two-phase session (positioned pre-publish) waits via
        ``session.wait``; a missing session falls back to the legacy single-call
        consumer for that topic. All waits run on short-lived worker threads
        (``handle`` itself already runs off the dispatch loop, OMN-13010). All
        sessions are closed on the way out; a losing wait unblocks when its
        session is closed and its thread ends no later than its own timeout —
        the executor is shut down without joining so the winner returns
        immediately.
        """
        waiters: dict[str, Callable[[], dict[str, Any] | None]] = {}
        for topic, session in sessions.items():
            if session is not None:
                waiters[topic] = functools.partial(
                    session.wait, correlation_id, timeout_seconds
                )
            else:
                waiters[topic] = functools.partial(
                    self._consumer, topic, correlation_id, timeout_seconds
                )

        payload: dict[str, Any] | None = None
        executor = ThreadPoolExecutor(
            max_workers=len(waiters), thread_name_prefix="roi-terminal-wait"
        )
        try:
            future_to_topic = {
                executor.submit(waiter): topic for topic, waiter in waiters.items()
            }
            try:
                for future in as_completed(
                    future_to_topic,
                    timeout=timeout_seconds + _TERMINAL_RACE_GRACE_SECONDS,
                ):
                    topic = future_to_topic[future]
                    try:
                        candidate = future.result()
                    except Exception as exc:
                        logger.warning(
                            "[roi-runner] terminal wait on %s raised for %s: %s",
                            topic,
                            correlation_id,
                            exc,
                        )
                        continue
                    if candidate is not None:
                        logger.debug(
                            "[roi-runner] terminal for %s delivered on %s",
                            correlation_id,
                            topic,
                        )
                        payload = candidate
                        break
            except TimeoutError:
                logger.warning(
                    "[roi-runner] terminal race exceeded its backstop for %s "
                    "(timeout_seconds=%s)",
                    correlation_id,
                    timeout_seconds,
                )
        finally:
            self._close_terminal_sessions(sessions)
            executor.shutdown(wait=False, cancel_futures=True)
        return payload

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
        # Factor-subset hash is arm-intrinsic -- computed once up front so every
        # row construction path (pack failure, publish failure, terminal timeout,
        # success) carries the same replay-audit hash.
        factor_subset_hash = _factor_subset_hash(arm.factor_subset)

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
                # Source the pack from the canonical builder. Failure stage
                # preserves the builder's class, including BUDGET_FAIL.
                (
                    context_pack_text,
                    context_pack_hash,
                    assemble_warnings,
                    pack_result_stage,
                ) = _build_context_pack(
                    factor_subset=arm.factor_subset,
                    artifact_content_map=request.artifact_content_map,
                    contract_hash=factor_subset_hash,
                )
                warnings.extend(assemble_warnings)

                if pack_result_stage != EnumFailureStage.NONE:
                    pack_failure_stage = pack_result_stage

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
                    factor_subset_hash=factor_subset_hash,
                    failure_stage=pack_failure_stage,
                    proof_class=EnumProofClass.RUNTIME_OBSERVED_ONLY,
                ),
                warnings,
            )

        # --- Step 2a: subscribe-before-publish (OMN-13012), BOTH terminals ---
        # Position one terminal consumer per terminal topic to the current end
        # BEFORE publishing the command. With the dispatch loop freed (OMN-13010)
        # generation completes in ~1s, so a single-call consumer that seeks AFTER
        # publishing skips past the already-emitted terminal (probe3). A two-phase
        # session captures the pre-publish offset here; the post-publish wait
        # resumes from it. The generation consumer routes contract_passed=False
        # benchmarks to the FAILED terminal topic, so both topics must be
        # positioned (OMN-13038). For a legacy single-call consumer (or the no-op
        # default) a topic's session stays None and the race falls back to
        # seek-at-wait for that topic.
        sessions: dict[str, TerminalConsumerSessionLike | None] = {
            topic: self._open_terminal_session(topic)
            for topic in (self._gen_terminal_topic, self._gen_terminal_failed_topic)
        }

        # --- Step 2a': register the correlation_id BEFORE publish (OMN-13118) ---
        # The injected long-lived correlator (omnibase_infra TerminalEventConsumer)
        # runs an always-on poll loop. Its per-correlation future must exist BEFORE
        # the command is published, or the loop reads the correlated terminal and
        # UNMATCHED-drops it (pending_keys=[]) ~before the post-publish wait would
        # register the cid — the strike-six wedge traced in
        # docs/evidence/2026-06-12-weekend-pass/experiments/probe4-stability/
        # diagnostic-rebuild8/FINDINGS.md. The two-phase contract is
        # open -> register -> publish -> wait; open() (Step 2a) and wait() (Step 3)
        # were wired but register() was folded into wait() (post-publish), so it is
        # hoisted here. Idempotent: wait() re-registering the same cid is a no-op.
        # Duck-typed because TerminalConsumerSessionLike (runtime_checkable) does
        # not declare register(); the runtime session has it, legacy/None sessions
        # skip (their seek-at-wait fallback needs no pre-registration).
        for session in sessions.values():
            if session is None:
                continue
            register = getattr(session, "register", None)
            if callable(register):
                register(correlation_id)

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
            self._close_terminal_sessions(sessions)
            return (
                ModelAttemptReductionRow(
                    run_id=request.run_id,
                    correlation_id=correlation_id,
                    task_id=task.task_id,
                    run_order=run_order,
                    context_factor_subset=arm.label,
                    context_pack_hash=context_pack_hash,
                    factor_subset_hash=factor_subset_hash,
                    failure_stage=EnumFailureStage.GENERATION,
                    proof_class=EnumProofClass.RUNTIME_OBSERVED_ONLY,
                ),
                warnings,
            )

        # --- Step 3: consume terminal generation event (block AFTER publish) ---
        # Race the correlated wait across BOTH terminal topics; the generation
        # consumer emits exactly one terminal per command, on the completed OR
        # the failed topic (OMN-13038). All sessions are closed on the way out.
        event_payload = self._await_first_terminal(
            sessions=sessions,
            correlation_id=correlation_id,
            timeout_seconds=request.generation_timeout_seconds,
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
                    factor_subset_hash=factor_subset_hash,
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
            factor_subset_hash=factor_subset_hash,
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
        factor_subset_hash: str,
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
                    factor_subset_hash=factor_subset_hash,
                    failure_stage=EnumFailureStage.GENERATION,
                    proof_class=EnumProofClass.RUNTIME_OBSERVED_ONLY,
                ),
                warnings,
            )

        # Unwrap ModelEventEnvelope if present: EventBusKafka.publish() wraps
        # the raw ModelGenerationBenchmark bytes in an envelope before writing
        # to Kafka, producing {"payload": {attempt_count, ...}, "envelope_id":
        # ..., "correlation_id": ...}.  The CID match succeeds because
        # correlation_id is copied to the top-level envelope field, but all
        # telemetry fields live under "payload".  Use the inner dict when a
        # "payload" key is present; otherwise the dict is already raw.
        # (OMN-13099 — probe4 degenerate-row defect)
        telemetry: dict[str, Any] = (
            event_payload["payload"]
            if isinstance(event_payload.get("payload"), dict)
            else event_payload
        )

        if "attempt_count" not in telemetry:
            return _fail_closed_row(
                "terminal event missing 'attempt_count' (fail-closed)"
            )

        try:
            attempt_count = int(telemetry["attempt_count"])
            prompt_tokens = int(telemetry.get("prompt_tokens", 0))
            completion_tokens = int(telemetry.get("completion_tokens", 0))
            cost_usd = float(telemetry.get("cost_inference_usd", 0.0))
        except (TypeError, ValueError) as exc:
            return _fail_closed_row(
                f"terminal event has non-numeric telemetry field ({exc})"
            )

        contract_passed: bool = bool(telemetry.get("contract_passed", False))
        first_pass_success: bool = bool(telemetry.get("first_pass_success", False))
        model_id: str = str(telemetry.get("model_id", ""))
        provider: str = str(telemetry.get("provider", ""))
        endpoint_class: str = str(telemetry.get("endpoint_class", ""))
        # routing_source is echoed back from the routing authority on the terminal
        # event when the generation consumer carries it; otherwise it is derived
        # from the endpoint class so every row carries a provenance string rather
        # than a silent empty (BAC plan line 111). Never hardcoded here.
        routing_source: str = str(telemetry.get("routing_source", ""))
        if not routing_source and endpoint_class:
            routing_source = f"routing_tier:{endpoint_class}"

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
                factor_subset_hash=factor_subset_hash,
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
                routing_source=routing_source,
                proof_class=EnumProofClass.RUNTIME_OBSERVED_ONLY,
            ),
            warnings,
        )

    # ------------------------------------------------------------------
    # Emit result event
    # ------------------------------------------------------------------

    def _emit_result(self, result: ModelContextRoiRunResult) -> None:
        # Route the run to the matching terminal: a run where every cell failed
        # (or that produced no cells at all) is a whole-run failure and is
        # published on the FAILED terminal topic; otherwise the run carries at
        # least one usable row and is published on the COMPLETED terminal topic.
        # Both terminals carry the same ModelContextRoiRunResult payload, so the
        # projection reducer (node_projection_context_roi) materialises rows from
        # either terminal. Without this routing the declared FAILED topic was
        # dead and a fully-failed run was never projected, wedging the N-arm
        # battery with zero usable rows for failed runs (OMN-13645).
        run_failed = (
            result.total_trials == 0 or result.failed_trials == result.total_trials
        )
        topic = self._topic_failed if run_failed else self._topic_completed
        if not topic:
            logger.warning(
                "[roi-runner] no %s topic configured; result not emitted",
                "failed" if run_failed else "completed",
            )
            return
        try:
            payload = json.dumps(result.model_dump()).encode("utf-8")
            self._publisher(topic, payload)
        except Exception as exc:
            logger.warning("[roi-runner] emit result to %s failed: %s", topic, exc)


__all__ = [
    "HandlerContextRoiRunner",
    "_build_context_pack",
    "_factor_str_to_enum",
    "_factor_subset_hash",
    "_sha256",
]
