# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Reconcile typed facts, preserve uncertain work, and publish run evidence."""

import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Literal

from omnimarket.events.worktree_reconcile import (
    EnumWorktreeKind as Kind,
)
from omnimarket.events.worktree_reconcile import (
    EnumWorktreeReconcileDecision as Decision,
)
from omnimarket.events.worktree_reconcile import (
    ModelWorktreeDecisionRecord,
    ModelWorktreeFacts,
    ModelWorktreeReconcileCommand,
    ModelWorktreeReconcileDecidedEvent,
    ModelWorktreeReconcileRequest,
    ModelWorktreeReconcileRunCompletedEvent,
    ModelWorktreeReconcileRunResult,
)
from omnimarket.nodes.node_worktree_reconcile_effect.protocols import (
    ProtocolClock,
    ProtocolWorktreeDecider,
    ProtocolWorktreeEventPublisher,
    ProtocolWorktreeFactsProbe,
    ProtocolWorktreePinner,
    ProtocolWorktreeRemover,
)
from omnimarket.worktree_reconcile.rules import (
    apply_model_verdicts,
    live_reason,
    reconcile,
)

# Repositories acted on at once. Trees of one repository stay serial (they share
# its git metadata and locks); different repositories are independent, and on a
# loaded host the per-tree re-probe before each removal dominates a run.
EXECUTE_WORKERS = 4


class HandlerWorktreeReconcile:
    def __init__(
        self,
        *,
        probe: ProtocolWorktreeFactsProbe,
        decider: ProtocolWorktreeDecider,
        pinner: ProtocolWorktreePinner,
        remover: ProtocolWorktreeRemover,
        clock: ProtocolClock,
        publisher: ProtocolWorktreeEventPublisher,
        decided_topic: str,
        completed_topic: str,
    ) -> None:
        self._probe = probe
        self._decider = decider
        self._pinner = pinner
        self._remover = remover
        self._clock = clock
        self._publisher = publisher
        self._decided_topic = decided_topic
        self._completed_topic = completed_topic

    @property
    def handler_type(self) -> Literal["NODE_HANDLER"]:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> Literal["EFFECT"]:
        return "EFFECT"

    def handle(
        self, command: ModelWorktreeReconcileCommand
    ) -> ModelWorktreeReconcileRunResult:
        started = self._clock.now()
        errors: list[str] = []
        try:
            facts = self._probe.discover(command, started)
        except Exception as exc:
            facts = ()
            errors.append(f"discovery_failed:{type(exc).__name__}:{str(exc)[:200]}")
        decisions = reconcile(
            ModelWorktreeReconcileRequest(facts=facts, policy=command.policy)
        )
        ambiguous = {
            row.path
            for row in decisions.decisions
            if row.decision == Decision.NEEDS_HUMAN
        }
        if ambiguous and command.decider_command:
            try:
                verdicts = self._decider.decide(
                    command.decider_command,
                    tuple(f for f in facts if f.path in ambiguous),
                )
                decisions = apply_model_verdicts(decisions, facts, verdicts)
            except Exception as exc:
                # An unavailable or malformed model never removes a tree.
                errors.append(f"decider_failed:{type(exc).__name__}")
        by_path = {fact.path: fact for fact in facts}
        rows = decisions.decisions
        done: dict[int, ModelWorktreeReconcileDecidedEvent] = {}
        lock = threading.Lock()
        published = 0

        def run_group(indices: list[int]) -> None:
            nonlocal published
            for index in indices:
                fact = by_path[rows[index].path]
                try:
                    event = self._execute(command, fact, rows[index])
                except Exception as exc:
                    # One row's surprise must not lose the other groups' record.
                    event = ModelWorktreeReconcileDecidedEvent(
                        correlation_id=command.correlation_id,
                        host=command.host,
                        facts=fact,
                        decision=rows[index],
                        outcome="failed",
                        error=type(exc).__name__,
                    )
                with lock:
                    done[index] = event
                    # Publish in decision order, as soon as the prefix is complete,
                    # so a run cut short still leaves every finished row behind.
                    while published in done:
                        self._publisher.publish(self._decided_topic, done[published])
                        published += 1

        # Every worktree of one clone shares its remotes and so its slug; keying on
        # (root, slug) never runs two trees of one clone at once. A tree with no
        # slug is its own group.
        groups: dict[tuple[str, str], list[int]] = {}
        for index, row in enumerate(rows):
            fact = by_path[row.path]
            key = (fact.root, fact.repo_slug) if fact.repo_slug else ("", fact.path)
            groups.setdefault(key, []).append(index)
        with ThreadPoolExecutor(max_workers=EXECUTE_WORKERS) as pool:
            for future in [pool.submit(run_group, g) for g in groups.values()]:
                future.result()
        events = [done[index] for index in range(len(rows))]
        completed = ModelWorktreeReconcileRunCompletedEvent(
            correlation_id=command.correlation_id,
            host=command.host,
            started_at=started,
            finished_at=self._clock.now(),
            scanned=len(events),
            removed=sum(e.outcome == "removed" for e in events),
            pinned_and_removed=sum(e.outcome == "pinned_and_removed" for e in events),
            kept=sum(e.outcome in ("kept", "dry_run") for e in events),
            needs_human=sum(
                e.decision.decision == Decision.NEEDS_HUMAN for e in events
            ),
            failures=len(errors)
            + sum(e.error is not None or bool(e.facts.probe_errors) for e in events),
            freed_bytes=sum(
                e.facts.size_bytes or 0
                for e in events
                if e.outcome in ("removed", "pinned_and_removed")
            ),
            needs_human_paths=tuple(
                e.facts.path
                for e in events
                if e.decision.decision == Decision.NEEDS_HUMAN
            ),
        )
        self._publisher.publish(self._completed_topic, completed)
        return ModelWorktreeReconcileRunResult(
            decided_events=tuple(events),
            completed_event=completed,
            errors=tuple(errors),
        )

    def _execute(
        self,
        command: ModelWorktreeReconcileCommand,
        facts: ModelWorktreeFacts,
        row: ModelWorktreeDecisionRecord,
    ) -> ModelWorktreeReconcileDecidedEvent:
        outcome: Literal[
            "dry_run", "removed", "pinned_and_removed", "kept", "needs_human", "failed"
        ] = "kept"
        error = None
        if row.decision == Decision.NEEDS_HUMAN:
            outcome = "needs_human"
        elif row.decision in (Decision.REMOVE, Decision.PIN_AND_REMOVE):
            if not command.execute:
                outcome = "dry_run"
            else:
                try:
                    # Validate before pinning too: do not pin a changed HEAD.
                    fresh = self._probe.revalidate(facts, self._clock.now())
                    if not self._safe(command, facts, fresh, require_remote=False):
                        row = row.model_copy(
                            update={
                                "decision": Decision.KEEP,
                                "reasons": ("revalidation_refused",),
                            }
                        )
                    else:
                        if row.decision == Decision.PIN_AND_REMOVE:
                            try:
                                pinned = bool(command.pin_command) and self._pinner.pin(
                                    command.pin_command or [], fresh
                                )
                            except Exception:
                                pinned = False
                            if not pinned:
                                row = row.model_copy(
                                    update={
                                        "decision": Decision.KEEP,
                                        "reasons": ("pin_failed",),
                                    }
                                )
                                error = "pin_failed"
                        if row.decision != Decision.KEEP:
                            if row.decision == Decision.PIN_AND_REMOVE:
                                fresh = self._probe.revalidate(facts, self._clock.now())
                            if not self._safe(
                                command, facts, fresh, require_remote=True
                            ):
                                row = row.model_copy(
                                    update={
                                        "decision": Decision.KEEP,
                                        "reasons": ("revalidation_refused",),
                                    }
                                )
                            else:
                                self._remover.remove(fresh, self._clock.now())
                                outcome = (
                                    "pinned_and_removed"
                                    if row.decision == Decision.PIN_AND_REMOVE
                                    else "removed"
                                )
                except Exception as exc:
                    outcome = "failed"
                    error = type(exc).__name__
        return ModelWorktreeReconcileDecidedEvent(
            correlation_id=command.correlation_id,
            host=command.host,
            facts=facts,
            decision=row,
            outcome=outcome,
            error=error,
        )

    @staticmethod
    def _safe(
        command: ModelWorktreeReconcileCommand,
        old: ModelWorktreeFacts,
        new: ModelWorktreeFacts,
        *,
        require_remote: bool,
    ) -> bool:
        if not new.facts_complete or new.dirty or live_reason(new, command.policy):
            return False
        if (old.path, old.root, old.kind, old.head_sha, old.branch) != (
            new.path,
            new.root,
            new.kind,
            new.head_sha,
            new.branch,
        ):
            return False
        if new.kind == Kind.ORPHAN_DIR:
            return new.orphan_empty
        if new.kind == Kind.STANDALONE_CLONE:
            return command.policy.allow_standalone_clone_removal and (
                not require_remote
                or (
                    new.head_on_remote
                    and new.commits_not_on_remote == 0
                    and new.local_branches_all_on_remote
                )
            )
        # A REMOVE justified by remote/merge evidence must still have it.
        return (
            not (old.head_on_remote or old.content_merged)
            or new.head_on_remote
            or new.content_merged
        )
