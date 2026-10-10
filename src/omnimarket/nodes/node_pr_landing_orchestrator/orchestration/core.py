# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One orchestrator leg: stored row plus one message to next row plus emissions.

This is the whole decision half of node_pr_landing_orchestrator (OMN-19829),
written against revision 1 of plan 5.1 (public knowledge-base,
plans/2026-09-27-pr-landing-state-machine-revision.md). The transitions
themselves are the reducer's (T6); this module owns everything the revision
assigns to the orchestrator:

* section 6: every autobind prompt and every reconciliation tick issues a
  conditional ``read_pr_state``, and its answer becomes observations keyed by
  the per-PR read sequence (:mod:`.snapshot`). A stale or duplicate answer is
  dropped whole;
* OBSERVED's evaluation, issued at once whenever a step leaves the row there;
* the in-row outbox (F6, F8, F9) and one GitHub effect in flight per PR (R4),
  with every arm or enqueue carrying its expected head (the intent's head) and
  passed through node_pr_arm_gate_compute before it is queued. A withheld arm is
  never queued, so the row's ``armed`` flag is cleared with it;
* before that gate, the ledger's gate facts (OMN-20866, :mod:`.gate_facts`): a
  green head of a PR a HOLD row holds, of a lab-proof repository with no PASS
  lab proof for the head, or whose facts cannot be read is kept pending, its
  reason named on the transition, and read again on the next poll;
* the completion bound per state entry (R2a, R2b): applied on the tick, tagged
  (episode, state_entry_generation), never in PARKED, once per entry;
* the events: one transitioned per transition with the orchestrator-owned
  per-key ``seq``; agent-needed once per (head, reason); one merged or closed
  terminal per (PR, episode) (P4, F9, F10).

The function is deterministic but for one read: time comes only from the
message, ids are derived from the row and the message, the in-process nodes it
calls are pure, and the gate facts port is read only for a green head, its
answer recorded on the transition as the withheld reason. The handler wraps it
in the ``state_io`` compare-and-set with retry.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from uuid import UUID, uuid5

from pydantic import BaseModel

from omnimarket.events.pr_arm_gate import (
    EnumArmDecision,
    ModelArmCandidate,
    ModelArmGatePolicy,
    ModelArmGateRequest,
)
from omnimarket.events.pr_head_check.enum_head_check_verdict import (
    EnumHeadCheckVerdict,
)
from omnimarket.events.pr_landing.model_pr_landing_check_attempt import (
    ModelPrLandingCheckAttempt,
)
from omnimarket.events.pr_landing_companion import (
    EnumPrLandingCompanionOp,
    EnumPrLandingCompanionOutcomeKind,
)
from omnimarket.events.pr_landing_github.enum_pr_landing_github_mode import (
    EnumPrLandingGithubMode,
)
from omnimarket.events.pr_landing_github.enum_pr_landing_github_operation import (
    EnumPrLandingGithubOperation,
)
from omnimarket.events.pr_landing_github.model_pr_landing_github_request import (
    ModelPrLandingGithubRequest,
)
from omnimarket.events.pr_landing_reduce import ModelPrLandingReduceInput
from omnimarket.events.pr_lifecycle_fix.model_fix_command import (
    EnumPrBlockReason,
    ModelPrLifecycleFixCommand,
)
from omnimarket.events.pr_state import EnumPrState
from omnimarket.events.topics import (
    PR_LANDING_COMPANION_OUTCOME_TOPIC_V1,
    PR_LANDING_GITHUB_COMPLETED_TOPIC_V1,
    PR_MERGED_TOPIC_V1,
)
from omnimarket.nodes.node_pr_landing_orchestrator.models.enum_pr_landing_arm_method import (
    EnumPrLandingArmMethod,
)
from omnimarket.nodes.node_pr_landing_orchestrator.models.enum_pr_landing_companion_outcome import (
    EnumPrLandingCompanionOutcome,
)
from omnimarket.nodes.node_pr_landing_orchestrator.models.enum_pr_landing_companion_status import (
    EnumPrLandingCompanionStatus,
)
from omnimarket.nodes.node_pr_landing_orchestrator.models.enum_pr_landing_intent_kind import (
    EnumPrLandingIntentKind,
)
from omnimarket.nodes.node_pr_landing_orchestrator.models.enum_pr_landing_observation_kind import (
    EnumPrLandingObservationKind,
)
from omnimarket.nodes.node_pr_landing_orchestrator.models.enum_pr_landing_state import (
    EnumPrLandingState,
)
from omnimarket.nodes.node_pr_landing_orchestrator.models.model_pr_landing_agent_needed import (
    ModelPrLandingAgentNeeded,
)
from omnimarket.nodes.node_pr_landing_orchestrator.models.model_pr_landing_closed import (
    ModelPrLandingClosed,
)
from omnimarket.nodes.node_pr_landing_orchestrator.models.model_pr_landing_gate_facts import (
    EnumPrLandingWithheldReason,
    ModelPrLandingGateDecision,
)
from omnimarket.nodes.node_pr_landing_orchestrator.models.model_pr_landing_ingress import (
    ModelPrLandingAutobindPrompt,
    ModelPrLandingCompanionOutcomeIngress,
    ModelPrLandingGithubCompletedIngress,
    ModelPrLandingGithubFailedIngress,
    ModelPrLandingMergedIngress,
    ModelPrLandingObservedPrompt,
    ModelPrLandingReconcileCommand,
    PrLandingOrchestratorInput,
)
from omnimarket.nodes.node_pr_landing_orchestrator.models.model_pr_landing_intent import (
    ModelPrLandingIntent,
)
from omnimarket.nodes.node_pr_landing_orchestrator.models.model_pr_landing_merged import (
    ModelPrLandingMerged,
)
from omnimarket.nodes.node_pr_landing_orchestrator.models.model_pr_landing_observation import (
    ModelPrLandingObservation,
    landing_key,
)
from omnimarket.nodes.node_pr_landing_orchestrator.models.model_pr_landing_state import (
    ModelPrLandingState,
)
from omnimarket.nodes.node_pr_landing_orchestrator.models.model_pr_landing_transitioned import (
    ModelPrLandingTransitioned,
)
from omnimarket.nodes.node_pr_landing_orchestrator.models.model_pr_landing_workflow_row import (
    ModelPrLandingAgentNeededKey,
    ModelPrLandingCheckRunRef,
    ModelPrLandingInFlight,
    ModelPrLandingWorkflowRow,
)
from omnimarket.nodes.node_pr_landing_orchestrator.orchestration.gate_facts import (
    decide_gate,
)
from omnimarket.nodes.node_pr_landing_orchestrator.orchestration.outbox import (
    PR_LANDING_NAMESPACE,
    add_intents,
    companion_command_id,
    effect_correlation_id,
    next_sendable,
)
from omnimarket.nodes.node_pr_landing_orchestrator.orchestration.ports import (
    ProtocolPrLandingArmGate,
    ProtocolPrLandingGateFacts,
    ProtocolPrLandingHeadCheckClassifier,
    ProtocolPrLandingReducer,
    call_reducer,
)
from omnimarket.nodes.node_pr_landing_orchestrator.orchestration.snapshot import (
    snapshot_observations,
)

# Per-state completion bounds, from the contract's state_machine timeout_ms
# (fixture state_bounds_ms). PARKED has none (R2a); NEEDS_AGENT starts at 24h.
DEFAULT_STATE_BOUNDS: Mapping[EnumPrLandingState, timedelta | None] = {
    EnumPrLandingState.OBSERVED: timedelta(milliseconds=300_000),
    EnumPrLandingState.PARKED: None,
    EnumPrLandingState.COMPANION_PENDING: timedelta(milliseconds=1_800_000),
    EnumPrLandingState.COMPANION_OPEN: timedelta(milliseconds=7_200_000),
    EnumPrLandingState.CHECKS_PENDING: timedelta(milliseconds=7_200_000),
    EnumPrLandingState.READY: timedelta(milliseconds=900_000),
    EnumPrLandingState.ARMED: timedelta(milliseconds=14_400_000),
    EnumPrLandingState.NEEDS_AGENT: timedelta(milliseconds=86_400_000),
}

_TERMINAL = frozenset({EnumPrLandingState.MERGED, EnumPrLandingState.CLOSED})
_RECONCILED = frozenset(
    state
    for state in EnumPrLandingState
    if state not in _TERMINAL  # every non-terminal row, PARKED included (section 6)
)
# OBSERVED evaluates at once; a well-formed reducer leaves OBSERVED in one step.
_MAX_EVALUATIONS = 4
GATE_RELATION_RE = re.compile(r"^[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*$")
GATE_ENV_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")

_OPERATION_BY_KIND: Mapping[EnumPrLandingIntentKind, EnumPrLandingGithubOperation] = {
    EnumPrLandingIntentKind.GITHUB_ARM: EnumPrLandingGithubOperation.ARM_AUTO_MERGE,
    EnumPrLandingIntentKind.GITHUB_ENQUEUE: EnumPrLandingGithubOperation.ENQUEUE,
    EnumPrLandingIntentKind.GITHUB_DISARM: EnumPrLandingGithubOperation.DISARM,
    EnumPrLandingIntentKind.GITHUB_RERUN: EnumPrLandingGithubOperation.RERUN_RUNS,
    EnumPrLandingIntentKind.GITHUB_UPDATE_BRANCH: EnumPrLandingGithubOperation.UPDATE_BRANCH,
    EnumPrLandingIntentKind.GITHUB_READ_HEAD_CHECKS: EnumPrLandingGithubOperation.READ_HEAD_CHECKS,
    EnumPrLandingIntentKind.GITHUB_READ_PR_STATE: EnumPrLandingGithubOperation.READ_PR_STATE,
}
_READ_OPERATIONS = frozenset(
    {
        EnumPrLandingGithubOperation.READ_HEAD_CHECKS,
        EnumPrLandingGithubOperation.READ_PR_STATE,
    }
)
# GitHub mergeable_state values under which a green head is not yet ready.
_MERGE_STATES_NOT_SETTLED = frozenset({"unknown", "blocked"})
_OP_BY_COMPANION_KIND: Mapping[EnumPrLandingIntentKind, EnumPrLandingCompanionOp] = {
    EnumPrLandingIntentKind.COMPANION_DERIVE: EnumPrLandingCompanionOp.DERIVE,
    EnumPrLandingIntentKind.COMPANION_REGENERATE: EnumPrLandingCompanionOp.REGENERATE,
    EnumPrLandingIntentKind.COMPANION_VERIFY: EnumPrLandingCompanionOp.VERIFY,
}


@dataclass(frozen=True)
class PrLandingGateFactsConfig:
    """Where the arm decision reads its ledger facts, and which repos need a lab pass.

    Built from the contract's ``landing_gate_facts`` block (OMN-20866).
    """

    # Rule 24: a runtime-affecting PR in these repositories lands only with a
    # PASS lab proof for its head. The workflow reads no changed-file class, so
    # every PR of these repositories counts as runtime-affecting (unknown
    # fails closed as runtime).
    lab_proof_repos: frozenset[str]
    # The ledger projection must have projected a row within this bound, else a
    # hold written since could not be seen and the holds read UNKNOWN.
    ledger_freshness_bound: timedelta
    dsn_env: str
    hold_state_relation: str
    ledger_rows_relation: str
    lab_proof_receipts_relation: str

    def __post_init__(self) -> None:
        for name in (
            "hold_state_relation",
            "ledger_rows_relation",
            "lab_proof_receipts_relation",
        ):
            if not GATE_RELATION_RE.fullmatch(getattr(self, name)):
                msg = f"{name} must be a lowercase schema.table identifier"
                raise ValueError(msg)
        if not GATE_ENV_RE.fullmatch(self.dsn_env):
            msg = "dsn_env must be an environment variable name"
            raise ValueError(msg)


@dataclass(frozen=True)
class PrLandingOrchestratorConfig:
    """Operator-controlled knobs. Defaults are shadow mode (plan 5.5, wave 3).

    The handler builds this from the ``landing_config`` block of the node's
    contract.yaml (OMN-20866); these defaults apply only where a caller builds
    the config itself.
    """

    # The mode of every GitHub mutation and companion command, unless the
    # repository is named in github_mode_by_repository.
    github_mode: EnumPrLandingGithubMode = EnumPrLandingGithubMode.DRY_RUN
    github_mode_by_repository: Mapping[str, EnumPrLandingGithubMode] = field(
        default_factory=dict
    )
    # Mutations that stay dry_run in every repository, whatever its mode: the
    # classes another owner still acts on (no dual owner).
    dry_run_operations: frozenset[EnumPrLandingGithubOperation] = frozenset()
    arm_policy: ModelArmGatePolicy = field(default_factory=ModelArmGatePolicy)
    head_checks_poll_interval: timedelta = timedelta(minutes=2)
    state_bounds: Mapping[EnumPrLandingState, timedelta | None] = field(
        default_factory=lambda: dict(DEFAULT_STATE_BOUNDS)
    )
    # Base branches the workflow lands into; any other base parks the row.
    served_base_refs: frozenset[str] = frozenset({"dev", "main"})
    # Repositories whose PRs need no change-control companion. Every repo that
    # publishes the autobind command needs one unless it is named here.
    companion_exempt_repos: frozenset[str] = frozenset()
    # Repositories landing through a merge queue; every other repo arms auto-merge.
    queue_repos: frozenset[str] = frozenset()
    # Until the producer builds op=verify (T10 treats it as derive), the tick
    # does not verify open companions; companion merged waits for T12.
    verify_open_companions_on_tick: bool = False
    # Repositories whose base branch requires no conversation resolution, so an
    # unresolved review thread cannot block the merge an arm requests. The arm
    # gate reads that as zero blocking threads; every other repository leaves
    # the count unknown and the gate withholds.
    review_threads_not_required_repos: frozenset[str] = frozenset()
    # Repositories whose PR-state observations (the PR watcher's feed) prompt
    # a read, as a push prompt does. Every other repository's observation is
    # dropped.
    observed_prompt_repos: frozenset[str] = frozenset()
    # The ledger gate facts every green head is checked against before an arm
    # (OMN-20866). None, the default of a hand-built config, reads none; the
    # contract declares them, so the runtime always reads them.
    gate_facts: PrLandingGateFactsConfig | None = None

    def companion_required(self, repository: str) -> bool:
        return repository not in self.companion_exempt_repos

    def github_mode_for(self, repository: str) -> EnumPrLandingGithubMode:
        return self.github_mode_by_repository.get(repository, self.github_mode)

    def mutation_mode(
        self, repository: str, operation: EnumPrLandingGithubOperation
    ) -> EnumPrLandingGithubMode:
        """The mode one GitHub mutation is sent with (reads are always enforce)."""
        if operation in self.dry_run_operations:
            return EnumPrLandingGithubMode.DRY_RUN
        return self.github_mode_for(repository)

    def companion_dry_run(self, repository: str) -> bool:
        return self.github_mode_for(repository) is EnumPrLandingGithubMode.DRY_RUN

    def arm_method(self, repository: str) -> EnumPrLandingArmMethod:
        if repository in self.queue_repos:
            return EnumPrLandingArmMethod.QUEUE
        return EnumPrLandingArmMethod.AUTO_MERGE


@dataclass(frozen=True)
class PrLandingOrchestratorPorts:
    """The pure nodes one leg calls in process."""

    reducer: ProtocolPrLandingReducer
    arm_gate: ProtocolPrLandingArmGate
    classifier: ProtocolPrLandingHeadCheckClassifier
    # The read of the ledger gate facts; None withholds every green head of a
    # config that declares the gate (fail closed).
    gate_facts: ProtocolPrLandingGateFacts | None = None


@dataclass(frozen=True)
class PrLandingStepResult:
    """The row to persist (None: nothing to persist) and what to publish."""

    row: ModelPrLandingWorkflowRow | None
    emitted: tuple[BaseModel, ...] = ()
    dropped_reason: str | None = None


class _Leg:
    """Mutable working state of one leg. Never escapes :func:`run_leg`."""

    def __init__(
        self,
        row: ModelPrLandingWorkflowRow,
        *,
        now: datetime,
        config: PrLandingOrchestratorConfig,
        ports: PrLandingOrchestratorPorts,
    ) -> None:
        self.row = row
        self.now = now
        self.config = config
        self.ports = ports
        self.emitted: list[BaseModel] = []

    # ------------------------------------------------------------------ reduce

    async def apply(
        self,
        observation: ModelPrLandingObservation,
        *,
        withheld_reason: str | None = None,
    ) -> bool:
        """Reduce one observation; returns True when a transition was taken.

        The reducer owns the row it returns: ``seq``, the state-entry
        generation, ``armed`` and the in-row outbox with F6 applied. The
        orchestrator publishes the transition and then drains the entries that
        are not GitHub effects (agent-needed events and companion commands)
        in the same write, so only GitHub effects wait for the one-in-flight
        rule (R4).
        """
        before = self.row.landing
        output = await call_reducer(
            self.ports.reducer,
            ModelPrLandingReduceInput(state=before, observation=observation),
        )
        if output.trigger is None:
            return False
        after = output.state
        self.row = self.row.model_copy(update={"landing": after})
        self.emitted.append(
            ModelPrLandingTransitioned(
                repository=after.repository,
                pr_number=after.pr_number,
                head_sha=after.head_sha,
                seq=after.seq,
                from_state=before.state if before is not None else None,
                to_state=after.state,
                trigger=output.trigger,
                intents=output.intents,
                transitioned_at=observation.observed_at,
                withheld_reason=withheld_reason,
            )
        )
        self._terminal(before, after, observation.observed_at)
        self._drain_local(before, observation.observed_at)
        return True

    async def apply_and_evaluate(
        self,
        observation: ModelPrLandingObservation,
        *,
        withheld_reason: str | None = None,
    ) -> None:
        moved = await self.apply(observation, withheld_reason=withheld_reason)
        evaluations = 0
        while (
            moved
            and self.row.landing is not None
            and self.row.landing.state is EnumPrLandingState.OBSERVED
            and evaluations < _MAX_EVALUATIONS
        ):
            evaluations += 1
            moved = await self.apply(self._evaluation(observation.observed_at))

    def _evaluation(self, at: datetime) -> ModelPrLandingObservation:
        landing = self.row.landing
        assert landing is not None  # only called while the row sits in OBSERVED
        return ModelPrLandingObservation(
            repository=landing.repository,
            pr_number=landing.pr_number,
            landing_key=landing_key(landing.repository, landing.pr_number),
            head_sha=landing.head_sha,
            kind=EnumPrLandingObservationKind.EVALUATION,
            observed_at=at,
            source_topic="internal:pr-landing-evaluation",
            source_event_id=f"{self.row.landing_key}:evaluation:{landing.seq}",
            ticket_ids=landing.ticket_ids,
            companion_required=self.config.companion_required(landing.repository),
            base_served=(self.row.base_ref or "") in self.config.served_base_refs,
        )

    def _drain_local(self, before: ModelPrLandingState | None, at: datetime) -> None:
        """Send every outbox entry that is not a GitHub effect, in order (F8).

        agent_needed becomes the agent-needed event, deduplicated per (head,
        reason); companion intents become the autobind command with an op. A
        companion arm (``github.arm`` with ``target_pr``) is a companion write,
        which keeps the producer's credential (plan 5.4), so it goes out as
        ``companion.verify``, whose repair arms the companion.
        """
        landing = self.row.landing
        if landing is None:
            return
        kept: list[ModelPrLandingIntent] = []
        verify_sent = False
        for entry in landing.outbox:
            if entry.kind is EnumPrLandingIntentKind.AGENT_NEEDED:
                self._agent_needed(entry, before, landing, at)
            elif entry.kind in _OP_BY_COMPANION_KIND or entry.target_pr is not None:
                if entry.kind is EnumPrLandingIntentKind.COMPANION_DERIVE or (
                    entry.kind is EnumPrLandingIntentKind.COMPANION_REGENERATE
                ):
                    self.companion_command(entry, at)
                elif not verify_sent:
                    verify_sent = True
                    self.companion_command(
                        entry.model_copy(
                            update={
                                "kind": EnumPrLandingIntentKind.COMPANION_VERIFY,
                                "target_pr": None,
                            }
                        ),
                        at,
                    )
            else:
                kept.append(entry)
        if len(kept) != len(landing.outbox):
            self.row = self.row.model_copy(
                update={"landing": landing.model_copy(update={"outbox": tuple(kept)})}
            )

    async def arm_method_for(
        self, verdict: EnumHeadCheckVerdict
    ) -> EnumPrLandingArmMethod | None:
        """The arm gate's answer for a green head, before the reducer writes an arm.

        The reducer arms only when the observation names a method; None is the
        gate withholding (plan 5.1: every arm or enqueue passes the gate first).
        """
        landing = self.row.landing
        if landing is None or verdict is not EnumHeadCheckVerdict.GREEN:
            return None
        if landing.draft or landing.held:
            return None
        decision = await self.ports.arm_gate.handle(
            ModelArmGateRequest(
                candidate=self._arm_candidate(landing),
                policy=self.config.arm_policy,
            )
        )
        if decision.decision is not EnumArmDecision.ARM:
            return None
        return self.config.arm_method(landing.repository)

    async def gate_decision(self, head_sha: str) -> ModelPrLandingGateDecision:
        """The ledger gate facts' answer for a green head (OMN-20866), fail-closed.

        A draft or held (title or label) row is parked by the reducer and
        never reaches an arm, so it needs no read.
        """
        landing = self.row.landing
        gate = self.config.gate_facts
        if gate is None or landing is None or landing.draft or landing.held:
            return ModelPrLandingGateDecision()
        port = self.ports.gate_facts
        if port is None:
            return ModelPrLandingGateDecision(
                withheld=EnumPrLandingWithheldReason.LEDGER_HOLDS_UNKNOWN,
                detail="no gate facts reader is wired",
            )
        try:
            facts = await port.read(
                landing.repository, landing.pr_number, head_sha, self.now
            )
        except Exception as exc:  # fail closed on any reader failure
            return ModelPrLandingGateDecision(
                withheld=EnumPrLandingWithheldReason.LEDGER_HOLDS_UNKNOWN,
                detail=f"{type(exc).__name__}: {exc}",
            )
        return decide_gate(
            facts,
            now=self.now,
            lab_proof_required=landing.repository in gate.lab_proof_repos,
            ledger_freshness_bound=gate.ledger_freshness_bound,
        )

    def _arm_candidate(self, state: ModelPrLandingState) -> ModelArmCandidate:
        companion_ok = (
            state.companion.status is EnumPrLandingCompanionStatus.MERGED
            or (
                state.companion.status is EnumPrLandingCompanionStatus.NONE
                and not self.config.companion_required(state.repository)
            )
        )
        merge_state = self.row.merge_state_status or state.merge_state
        return ModelArmCandidate(
            repo=state.repository,
            pr_number=state.pr_number,
            is_draft=state.draft,
            # The workflow reads no review threads: zero blocking threads only
            # where the base requires no conversation resolution, else unknown,
            # which withholds (fail closed).
            coderabbit_unresolved=0
            if state.repository in self.config.review_threads_not_required_repos
            else None,
            merge_state_status=merge_state.upper() if merge_state else None,
            # Only called for a GREEN verdict of the head-check classifier.
            head_check_verdict=EnumHeadCheckVerdict.GREEN,
            occ_companion_verified=companion_ok,
        )

    # ------------------------------------------------------------------ events

    def _agent_needed(
        self,
        intent: ModelPrLandingIntent,
        before: ModelPrLandingState | None,
        after: ModelPrLandingState,
        at: datetime,
    ) -> None:
        assert intent.agent_reason is not None  # the intent model guarantees it
        key = ModelPrLandingAgentNeededKey(
            head_sha=after.head_sha, reason=intent.agent_reason
        )
        if key in self.row.agent_needed_sent:
            return
        self.row = self.row.model_copy(
            update={"agent_needed_sent": (*self.row.agent_needed_sent, key)}
        )
        self.emitted.append(
            ModelPrLandingAgentNeeded(
                repository=after.repository,
                pr_number=after.pr_number,
                head_sha=after.head_sha,
                reason=intent.agent_reason,
                from_state=before.state if before is not None else after.state,
                detail=intent.detail,
                seq=after.seq,
                raised_at=at,
            )
        )

    def _terminal(
        self,
        before: ModelPrLandingState | None,
        after: ModelPrLandingState,
        at: datetime,
    ) -> None:
        if after.state not in _TERMINAL:
            return
        entered = (
            before is None
            or before.state is not after.state
            or (before.episode != after.episode)
        )
        if not entered or after.episode in self.row.terminal_episodes:
            return
        self.row = self.row.model_copy(
            update={"terminal_episodes": (*self.row.terminal_episodes, after.episode)}
        )
        if after.state is EnumPrLandingState.MERGED:
            self.emitted.append(
                ModelPrLandingMerged(
                    repository=after.repository,
                    pr_number=after.pr_number,
                    head_sha=after.head_sha,
                    seq=after.seq,
                    episode=after.episode,
                    merged_at=at,
                )
            )
        else:
            self.emitted.append(
                ModelPrLandingClosed(
                    repository=after.repository,
                    pr_number=after.pr_number,
                    head_sha=after.head_sha,
                    seq=after.seq,
                    episode=after.episode,
                    closed_at=at,
                )
            )

    def companion_command(self, intent: ModelPrLandingIntent, at: datetime) -> None:
        """A companion intent goes to the producer as the autobind command with an op."""
        op = _OP_BY_COMPANION_KIND[intent.kind]
        if intent.command_id is not None:
            correlation = companion_command_id(self.row.landing_key, intent.command_id)
        else:
            landing = self.row.landing
            entry = landing.state_entry_generation if landing is not None else 0
            correlation = uuid5(
                PR_LANDING_NAMESPACE,
                f"{self.row.landing_key}|{op.value}|{intent.head_sha or '-'}|{entry}|{at.isoformat()}",
            )
        tickets = self.row.landing.ticket_ids if self.row.landing is not None else ()
        self.emitted.append(
            ModelPrLifecycleFixCommand(
                correlation_id=correlation,
                pr_number=intent.pr_number,
                repo=intent.repository,
                block_reason=EnumPrBlockReason.RECEIPT_EVIDENCE_SOURCE_AUTOBIND,
                ticket_id=tickets[0] if tickets else None,
                op=op,
                dry_run=self.config.companion_dry_run(intent.repository),
                requested_at=at,
            )
        )

    # ---------------------------------------------------------------- dispatch

    def want_read(self) -> None:
        self.row = self.row.model_copy(update={"pending_read": True})

    def dispatch(self) -> None:
        """Send at most one GitHub effect, and only when none is in flight (R4)."""
        row = self.row
        if row.effect_in_flight is not None:
            return
        landing = row.landing
        if row.pending_read:
            read_id = row.reads_issued + 1
            intent = ModelPrLandingIntent(
                kind=EnumPrLandingIntentKind.GITHUB_READ_PR_STATE,
                repository=row.repository,
                pr_number=row.pr_number,
                head_sha=landing.head_sha if landing is not None else None,
            )
            self.row = row.model_copy(
                update={"pending_read": False, "reads_issued": read_id}
            )
            self._send(intent, read_id=read_id)
            return
        if landing is None:
            return
        index = next_sendable(
            landing.outbox,
            head_checks_read_at=row.head_checks_read_at,
            poll_interval=self.config.head_checks_poll_interval,
            now=self.now,
        )
        if index is None:
            return
        intent = landing.outbox[index]
        remaining = landing.outbox[:index] + landing.outbox[index + 1 :]
        # F8: the entry leaves the outbox in the same write that records it in flight.
        self.row = row.model_copy(
            update={"landing": landing.model_copy(update={"outbox": remaining})}
        )
        self._send(intent, read_id=None)

    def _send(self, intent: ModelPrLandingIntent, *, read_id: int | None) -> None:
        row = self.row
        number = row.dispatches + 1
        correlation = effect_correlation_id(row.landing_key, number, intent)
        request = self._request(intent, correlation)
        self.row = row.model_copy(
            update={
                "dispatches": number,
                "effect_in_flight": ModelPrLandingInFlight(
                    correlation_id=correlation,
                    intent=intent,
                    read_id=read_id,
                    sent_at=self.now,
                ),
            }
        )
        if request is not None:
            self.emitted.append(request)
        else:
            # Nothing addressable to send (a re-run with no known run id, a
            # GraphQL op before the node id is known): release the slot.
            self.row = self.row.model_copy(update={"effect_in_flight": None})

    def _request(
        self, intent: ModelPrLandingIntent, correlation: UUID
    ) -> ModelPrLandingGithubRequest | None:
        row = self.row
        landing = row.landing
        operation = _OPERATION_BY_KIND[intent.kind]
        mode = (
            EnumPrLandingGithubMode.ENFORCE
            if operation in _READ_OPERATIONS
            else self.config.mutation_mode(row.repository, operation)
        )
        fields: dict[str, object] = {
            "correlation_id": correlation,
            "operation": operation,
            "mode": mode,
            "repository": row.repository,
            "pr_number": row.pr_number,
            "head_sha": intent.head_sha
            or (landing.head_sha if landing is not None else None),
        }
        if operation is EnumPrLandingGithubOperation.READ_PR_STATE:
            fields["etag"] = row.pr_state_etag
        elif operation is EnumPrLandingGithubOperation.READ_HEAD_CHECKS:
            fields["etag"] = row.head_checks_etag
            # The base's required contexts decide which checks block (OMN-20866).
            fields["base_ref"] = row.base_ref
        elif operation is EnumPrLandingGithubOperation.RERUN_RUNS:
            run_ids = sorted(
                {ref.run_id for ref in row.check_runs if ref.check in intent.check_runs}
            )
            if not run_ids:
                return None
            fields["run_ids"] = tuple(run_ids)
        elif operation in (
            EnumPrLandingGithubOperation.ARM_AUTO_MERGE,
            EnumPrLandingGithubOperation.ENQUEUE,
            EnumPrLandingGithubOperation.DISARM,
        ):
            if row.pr_node_id is None:
                return None
            fields["pr_node_id"] = row.pr_node_id
            if operation is EnumPrLandingGithubOperation.DISARM:
                armed = landing.armed if landing is not None else None
                fields["armed_method"] = (
                    armed or EnumPrLandingArmMethod.AUTO_MERGE
                ).value
        if (
            fields["head_sha"] is None
            and operation is not EnumPrLandingGithubOperation.READ_PR_STATE
        ):
            return None
        return ModelPrLandingGithubRequest.model_validate(fields)

    # ------------------------------------------------------------------- bound

    async def expire_bound(self) -> None:
        """R2a, R2b: one expiry per (episode, state entry), never in PARKED."""
        landing = self.row.landing
        if landing is None:
            return
        bound = self.config.state_bounds.get(landing.state)
        if bound is None or self.now - landing.entered_state_at < bound:
            return
        tag = (landing.episode, landing.state_entry_generation)
        if self.row.bound_expired_for == tag:
            return
        self.row = self.row.model_copy(update={"bound_expired_for": tag})
        await self.apply_and_evaluate(
            ModelPrLandingObservation(
                repository=landing.repository,
                pr_number=landing.pr_number,
                landing_key=landing_key(landing.repository, landing.pr_number),
                head_sha=landing.head_sha,
                kind=EnumPrLandingObservationKind.BOUND_EXPIRED,
                observed_at=self.now,
                source_topic="internal:pr-landing-completion-bound",
                source_event_id=f"{self.row.landing_key}:bound:{tag[0]}:{tag[1]}",
                episode=landing.episode,
                state_entry_generation=landing.state_entry_generation,
            )
        )


def _new_row(
    repository: str, pr_number: int, now: datetime
) -> ModelPrLandingWorkflowRow:
    return ModelPrLandingWorkflowRow(
        repository=repository,
        pr_number=pr_number,
        landing_key=landing_key(repository, pr_number),
        updated_at=now,
    )


async def run_leg(
    row: ModelPrLandingWorkflowRow | None,
    message: PrLandingOrchestratorInput,
    *,
    config: PrLandingOrchestratorConfig,
    ports: PrLandingOrchestratorPorts,
) -> PrLandingStepResult:
    """Apply one consumed message to the stored row."""
    if isinstance(message, ModelPrLandingAutobindPrompt):
        return await _on_prompt(row, message, config=config, ports=ports)
    if isinstance(message, ModelPrLandingObservedPrompt):
        return await _on_observed(row, message, config=config, ports=ports)
    if row is None:
        return PrLandingStepResult(
            row=None, dropped_reason="no landing row for this PR"
        )
    if isinstance(message, ModelPrLandingReconcileCommand):
        return await _on_reconcile(row, message, config=config, ports=ports)
    if isinstance(message, ModelPrLandingMergedIngress):
        return await _on_merged(row, message, config=config, ports=ports)
    if isinstance(message, ModelPrLandingCompanionOutcomeIngress):
        return await _on_companion_outcome(row, message, config=config, ports=ports)
    if isinstance(message, ModelPrLandingGithubCompletedIngress):
        return await _on_github_completed(row, message, config=config, ports=ports)
    return await _on_github_failed(row, message, config=config, ports=ports)


def _finish(
    leg: _Leg, original: ModelPrLandingWorkflowRow | None
) -> PrLandingStepResult:
    row = leg.row.model_copy(update={"updated_at": leg.now})
    if (
        original is not None
        and row.model_copy(update={"updated_at": original.updated_at}) == original
        and not leg.emitted
    ):
        return PrLandingStepResult(row=None, dropped_reason="nothing changed")
    return PrLandingStepResult(row=row, emitted=tuple(leg.emitted))


async def _on_prompt(
    row: ModelPrLandingWorkflowRow | None,
    message: ModelPrLandingAutobindPrompt,
    *,
    config: PrLandingOrchestratorConfig,
    ports: PrLandingOrchestratorPorts,
) -> PrLandingStepResult:
    if message.is_companion_command:
        return PrLandingStepResult(
            row=None, dropped_reason="the workflow's own companion command"
        )
    now = message.requested_at
    start = row if row is not None else _new_row(message.repo, message.pr_number, now)
    leg = _Leg(start, now=now, config=config, ports=ports)
    leg.want_read()
    leg.dispatch()
    return _finish(leg, row)


async def _on_observed(
    row: ModelPrLandingWorkflowRow | None,
    message: ModelPrLandingObservedPrompt,
    *,
    config: PrLandingOrchestratorConfig,
    ports: PrLandingOrchestratorPorts,
) -> PrLandingStepResult:
    """I3 (OMN-20866): a watcher observation prompts a read for a named repository."""
    if message.repository not in config.observed_prompt_repos:
        return PrLandingStepResult(
            row=None, dropped_reason="repository not prompted by observations"
        )
    if row is None and message.state is not EnumPrState.OPEN:
        return PrLandingStepResult(
            row=None, dropped_reason="first sight of a PR that is not open"
        )
    landing = row.landing if row is not None else None
    if landing is not None and (
        landing.state is EnumPrLandingState.MERGED
        or (
            landing.state is EnumPrLandingState.CLOSED
            and message.state is not EnumPrState.OPEN
        )
    ):
        # A closed row reads again only when the watcher sees it open (G5).
        return PrLandingStepResult(row=None, dropped_reason="terminal row")
    now = message.observed_at
    if row is not None and now < row.updated_at:
        # An older observation redelivered: the row has seen later input.
        now = row.updated_at
    start = (
        row if row is not None else _new_row(message.repository, message.pr_number, now)
    )
    leg = _Leg(start, now=now, config=config, ports=ports)
    leg.want_read()
    leg.dispatch()
    return _finish(leg, row)


async def _on_reconcile(
    row: ModelPrLandingWorkflowRow,
    message: ModelPrLandingReconcileCommand,
    *,
    config: PrLandingOrchestratorConfig,
    ports: PrLandingOrchestratorPorts,
) -> PrLandingStepResult:
    landing = row.landing
    if landing is not None and landing.state not in _RECONCILED:
        return PrLandingStepResult(row=None, dropped_reason="terminal row")
    leg = _Leg(row, now=message.requested_at, config=config, ports=ports)
    await leg.expire_bound()
    landing = leg.row.landing
    if landing is not None and landing.state is EnumPrLandingState.CHECKS_PENDING:
        queued = any(
            i.kind is EnumPrLandingIntentKind.GITHUB_READ_HEAD_CHECKS
            for i in landing.outbox
        )
        in_flight = leg.row.effect_in_flight
        reading = in_flight is not None and (
            in_flight.intent.kind is EnumPrLandingIntentKind.GITHUB_READ_HEAD_CHECKS
        )
        if not queued and not reading and landing.head_sha is not None:
            # "verdict pending: read again after the poll interval" (row 33).
            leg.row = leg.row.model_copy(
                update={
                    "landing": landing.model_copy(
                        update={
                            "outbox": add_intents(
                                landing.outbox,
                                (
                                    ModelPrLandingIntent(
                                        kind=EnumPrLandingIntentKind.GITHUB_READ_HEAD_CHECKS,
                                        repository=landing.repository,
                                        pr_number=landing.pr_number,
                                        head_sha=landing.head_sha,
                                    ),
                                ),
                            )
                        }
                    )
                }
            )
    if (
        config.verify_open_companions_on_tick
        and landing is not None
        and landing.state is EnumPrLandingState.COMPANION_OPEN
    ):
        # Until the mirror publishes companion merges (T12), the tick verifies
        # every open companion so its merge is seen (section 6).
        leg.companion_command(
            ModelPrLandingIntent(
                kind=EnumPrLandingIntentKind.COMPANION_VERIFY,
                repository=landing.repository,
                pr_number=landing.pr_number,
                head_sha=landing.head_sha,
                detail=f"reconcile tick {message.tick_id}",
            ),
            message.requested_at,
        )
    leg.want_read()
    leg.dispatch()
    return _finish(leg, row)


async def _on_merged(
    row: ModelPrLandingWorkflowRow,
    message: ModelPrLandingMergedIngress,
    *,
    config: PrLandingOrchestratorConfig,
    ports: PrLandingOrchestratorPorts,
) -> PrLandingStepResult:
    if row.landing is None:
        return PrLandingStepResult(
            row=None, dropped_reason="merged before first snapshot"
        )
    merged_at = datetime.fromisoformat(message.merged_at)
    leg = _Leg(row, now=merged_at, config=config, ports=ports)
    await leg.apply_and_evaluate(
        ModelPrLandingObservation(
            repository=row.repository,
            pr_number=row.pr_number,
            landing_key=landing_key(row.repository, row.pr_number),
            kind=EnumPrLandingObservationKind.MERGED,
            observed_at=merged_at,
            source_topic=PR_MERGED_TOPIC_V1,
            source_event_id=message.event_id,
            ticket_ids=(message.ticket,) if message.ticket else (),
        )
    )
    return _finish(leg, row)


async def _on_companion_outcome(
    row: ModelPrLandingWorkflowRow,
    message: ModelPrLandingCompanionOutcomeIngress,
    *,
    config: PrLandingOrchestratorConfig,
    ports: PrLandingOrchestratorPorts,
) -> PrLandingStepResult:
    if row.landing is None or message.correlation_id is None:
        return PrLandingStepResult(
            row=None, dropped_reason="an outcome with no row or no command id"
        )
    # The outcome carries no time of its own; the row's latest time stands in.
    now = row.updated_at
    leg = _Leg(row, now=now, config=config, ports=ports)
    kind = _OUTCOME_KIND[message.kind]
    minted = kind is EnumPrLandingCompanionOutcome.MINTED
    declined = kind is EnumPrLandingCompanionOutcome.DECLINED
    if minted and message.occ_pr is None:
        return PrLandingStepResult(
            row=None, dropped_reason="a MINTED outcome with no occ_pr"
        )
    await leg.apply_and_evaluate(
        ModelPrLandingObservation(
            repository=row.repository,
            pr_number=row.pr_number,
            landing_key=landing_key(row.repository, row.pr_number),
            kind=EnumPrLandingObservationKind.COMPANION_OUTCOME,
            observed_at=now,
            source_topic=PR_LANDING_COMPANION_OUTCOME_TOPIC_V1,
            source_event_id=str(message.correlation_id),
            command_id=_answered_command_id(row, message.correlation_id),
            companion_outcome=kind,
            occ_pr=message.occ_pr if minted else None,
            # Unknown reads as not done, so verify and the companion arm follow.
            companion_stamped=bool(message.stamped) if minted else None,
            companion_armed=bool(message.armed) if minted else None,
            detail=(message.decline_reason or None) if declined else None,
        )
    )
    leg.dispatch()
    return _finish(leg, row)


_OUTCOME_KIND: Mapping[
    EnumPrLandingCompanionOutcomeKind, EnumPrLandingCompanionOutcome
] = {
    EnumPrLandingCompanionOutcomeKind.MINTED: EnumPrLandingCompanionOutcome.MINTED,
    EnumPrLandingCompanionOutcomeKind.DECLINED: EnumPrLandingCompanionOutcome.DECLINED,
    EnumPrLandingCompanionOutcomeKind.ERROR: EnumPrLandingCompanionOutcome.ERROR,
}


def _answered_command_id(row: ModelPrLandingWorkflowRow, correlation_id: UUID) -> str:
    """The reducer's command id this outcome answers (F5), or the raw id when none.

    The orchestrator sends each derive or regenerate with a correlation id
    derived from the reducer's command id, and the producer answers with it.
    An outcome for any other command keeps its raw id, which matches nothing,
    so the reducer drops it (row 14).
    """
    landing = row.landing
    in_flight = landing.companion.command_id if landing is not None else None
    if in_flight is not None and companion_command_id(row.landing_key, in_flight) == (
        correlation_id
    ):
        return in_flight
    return str(correlation_id)


def _answers_in_flight(
    row: ModelPrLandingWorkflowRow, correlation_id: UUID
) -> ModelPrLandingInFlight | None:
    in_flight = row.effect_in_flight
    if in_flight is None or in_flight.correlation_id != correlation_id:
        return None
    return in_flight


def _check_run_refs(
    message: ModelPrLandingGithubCompletedIngress,
) -> tuple[ModelPrLandingCheckRunRef, ...]:
    """Each check name's newest run id, parsed from its Actions details URL."""
    newest: dict[str, tuple[int, int]] = {}
    for fact in message.check_runs:
        url = fact.details_url or ""
        marker = "/actions/runs/"
        if marker not in url:
            continue
        tail = url.split(marker, 1)[1].split("/", 1)[0]
        if not tail.isdigit():
            continue
        seen = newest.get(fact.name)
        if seen is None or fact.check_run_id > seen[0]:
            newest[fact.name] = (fact.check_run_id, int(tail))
    return tuple(
        ModelPrLandingCheckRunRef(check=name, run_id=run_id)
        for name, (_check_run_id, run_id) in sorted(newest.items())
    )


async def _on_github_completed(
    row: ModelPrLandingWorkflowRow,
    message: ModelPrLandingGithubCompletedIngress,
    *,
    config: PrLandingOrchestratorConfig,
    ports: PrLandingOrchestratorPorts,
) -> PrLandingStepResult:
    in_flight = _answers_in_flight(row, message.correlation_id)
    if in_flight is None:
        return PrLandingStepResult(
            row=None, dropped_reason="not the effect in flight (duplicate or stale)"
        )
    # The effect's answer carries no clock of its own; the leg's time is when
    # the effect was sent, the latest time this row knows.
    leg = _Leg(
        row.model_copy(update={"effect_in_flight": None}),
        now=in_flight.sent_at,
        config=config,
        ports=ports,
    )
    operation = message.operation
    if operation is EnumPrLandingGithubOperation.READ_PR_STATE:
        await _apply_snapshot(leg, message, in_flight)
    elif operation is EnumPrLandingGithubOperation.READ_HEAD_CHECKS:
        await _apply_head_checks(leg, message)
    elif (
        operation
        in (
            EnumPrLandingGithubOperation.ARM_AUTO_MERGE,
            EnumPrLandingGithubOperation.ENQUEUE,
        )
        and message.mode is EnumPrLandingGithubMode.ENFORCE
    ):
        await leg.apply_and_evaluate(
            ModelPrLandingObservation(
                repository=row.repository,
                pr_number=row.pr_number,
                landing_key=landing_key(row.repository, row.pr_number),
                head_sha=message.head_sha,
                kind=EnumPrLandingObservationKind.ARMED_CONFIRMED,
                observed_at=leg.now,
                source_topic=PR_LANDING_GITHUB_COMPLETED_TOPIC_V1,
                source_event_id=str(message.correlation_id),
            )
        )
    leg.dispatch()
    return _finish(leg, row)


async def _apply_snapshot(
    leg: _Leg,
    message: ModelPrLandingGithubCompletedIngress,
    in_flight: ModelPrLandingInFlight,
) -> None:
    read_id = in_flight.read_id
    if read_id is None or read_id <= leg.row.reads_answered:
        return
    leg.row = leg.row.model_copy(
        update={"reads_answered": read_id, "pr_state_etag": message.etag}
    )
    fact = message.pr_state
    if fact is None:  # 304: unchanged since the last read, nothing newer to apply
        return
    leg.row = leg.row.model_copy(
        update={
            "pr_node_id": fact.pr_node_id,
            "base_ref": fact.base_ref,
            "merge_state_status": fact.mergeable_state,
        }
    )
    observations = snapshot_observations(
        landing=leg.row.landing,
        fact=fact,
        repository=leg.row.repository,
        read_id=read_id,
        observed_at=leg.now,
        source_event_id=str(message.correlation_id),
    )
    for observation in observations:
        await leg.apply_and_evaluate(observation)


async def _apply_head_checks(
    leg: _Leg, message: ModelPrLandingGithubCompletedIngress
) -> None:
    leg.row = leg.row.model_copy(
        update={"head_checks_etag": message.etag, "head_checks_read_at": leg.now}
    )
    if message.not_modified or message.head_sha is None:
        return  # unchanged: still pending; the tick reads again after the interval
    leg.row = leg.row.model_copy(update={"check_runs": _check_run_refs(message)})
    verdict = await leg.ports.classifier.classify(message, leg.row)
    if (
        verdict.verdict is EnumHeadCheckVerdict.GREEN
        and (leg.row.merge_state_status or "").lower() in _MERGE_STATES_NOT_SETTLED
    ):
        # Every blocking check passed, but GitHub has not computed the PR's
        # mergeability yet, or still reports it blocked (a required context not
        # posted yet): the head is still pending, read again after the interval.
        verdict = verdict.model_copy(update={"verdict": EnumHeadCheckVerdict.PENDING})
    withheld_reason: str | None = None
    if verdict.verdict is EnumHeadCheckVerdict.GREEN:
        gate = await leg.gate_decision(message.head_sha)
        if gate.withheld is not None:
            # A hold in force, no PASS lab proof for this head, or facts that
            # cannot be read: never armed. The head stays pending, its reason
            # named on the transition, and the next poll is a full read (no
            # etag) so a RELEASE or a new PASS is seen without a new push.
            verdict = verdict.model_copy(
                update={"verdict": EnumHeadCheckVerdict.PENDING}
            )
            withheld_reason = gate.reason_text
            leg.row = leg.row.model_copy(update={"head_checks_etag": None})
    arm_method = await leg.arm_method_for(verdict.verdict)
    await leg.apply_and_evaluate(
        ModelPrLandingObservation(
            repository=leg.row.repository,
            pr_number=leg.row.pr_number,
            landing_key=landing_key(leg.row.repository, leg.row.pr_number),
            head_sha=message.head_sha,
            kind=EnumPrLandingObservationKind.HEAD_CHECKS,
            observed_at=leg.now,
            source_topic=PR_LANDING_GITHUB_COMPLETED_TOPIC_V1,
            source_event_id=str(message.correlation_id),
            verdict=verdict.verdict,
            rerun_checks=verdict.rerun_checks,
            check_attempts=tuple(
                ModelPrLandingCheckAttempt(check=a.check, attempt=a.attempt)
                for a in verdict.check_attempts
            ),
            arm_method=arm_method,
        ),
        withheld_reason=withheld_reason,
    )


async def _on_github_failed(
    row: ModelPrLandingWorkflowRow,
    message: ModelPrLandingGithubFailedIngress,
    *,
    config: PrLandingOrchestratorConfig,
    ports: PrLandingOrchestratorPorts,
) -> PrLandingStepResult:
    in_flight = _answers_in_flight(row, message.correlation_id)
    if in_flight is None:
        return PrLandingStepResult(
            row=None, dropped_reason="not the effect in flight (duplicate or stale)"
        )
    # A failed effect frees the PR. Nothing is retried here: a failed read is
    # re-read by the next tick, and a failed arm leaves the row to its bound.
    leg = _Leg(
        row.model_copy(update={"effect_in_flight": None}),
        now=in_flight.sent_at,
        config=config,
        ports=ports,
    )
    leg.dispatch()
    return _finish(leg, row)


__all__: list[str] = [
    "DEFAULT_STATE_BOUNDS",
    "GATE_ENV_RE",
    "GATE_RELATION_RE",
    "PrLandingGateFactsConfig",
    "PrLandingOrchestratorConfig",
    "PrLandingOrchestratorPorts",
    "PrLandingStepResult",
    "run_leg",
]
