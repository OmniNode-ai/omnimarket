# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Fold one broker observation into the per-topic reaper records (OMN-21594, hot-loading plan A6).

Every move between states runs through core's FSM transition executor over the contract's
``topic_reaper_fsm``; the handler only reads the observation, works out the facts a guard reads (the
injected ``interval_elapsed``, ``unfinished_work_references``) and names the trigger. The clock is the
request's ``now``: nothing here reads the wall clock.

Guards, none of them overridable by the policy:

* a protected-prefix, baseline or control topic is never flagged;
* a topic a boot-set contract subscribes to or publishes is never flagged, and a flag on it is cleared;
* a topic whose evidence was not read is held where it is;
* a topic with a live group member or moving end offsets is never handed out and returns to flagged;
* a topic an unfinished-work reference names is not deleted while the reference stands;
* a deletion is handed out only with the topic id the broker reported, and only when the policy enforces.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

from omnibase_core.models.contracts.subcontracts.model_fsm_subcontract import (
    ModelFSMSubcontract,
)
from omnibase_core.utils.util_fsm_executor import execute_transition

from omnimarket.models.model_topic_reaper import (
    REAPER_POLICY_MISSING,
    EnumReaperState,
    ModelReaperDecision,
    ModelReaperDeleteTarget,
    ModelReaperObservation,
    ModelReaperTopicState,
    ModelTopicReaperPolicy,
    ModelTopicReaperRequest,
    ModelTopicReaperResult,
)
from omnimarket.nodes.contract_topics import contract_section

CONTRACT_PATH = Path(__file__).resolve().parent.parent / "contract.yaml"

TRIGGER_FLAG = "last_declarer_withdrawn_or_removed"
TRIGGER_UNFLAG = "contract_re_announced"
TRIGGER_AGE_OUT = "interval_and_quiet_observations_met"
TRIGGER_ACTIVITY = "member_or_offset_movement_observed"
TRIGGER_DELETE = "deletion_issued"
TRIGGER_CONFIRM = "deletion_conclusive"


def load_fsm(path: Path = CONTRACT_PATH) -> ModelFSMSubcontract:
    """Load the contract's ``state_machine`` block; fail closed when it is absent."""
    return ModelFSMSubcontract.model_validate(contract_section(path, "state_machine"))


class HandlerTopicReaper:
    """Observe the broker once; move each topic's record and name what may be deleted."""

    def __init__(self, contract_path: Path = CONTRACT_PATH) -> None:
        self._fsm = load_fsm(contract_path)

    def _fire(
        self, state: EnumReaperState, trigger: str, context: Mapping[str, object]
    ) -> EnumReaperState | None:
        """Run one transition through core's executor; ``None`` when the machine holds the state."""
        coro = execute_transition(
            self._fsm,
            current_state=state.value,
            trigger=trigger,
            context=dict(context),
        )
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            result = asyncio.run(coro)
        else:
            with ThreadPoolExecutor(max_workers=1) as pool:
                result = pool.submit(asyncio.run, coro).result()
        if not result.success or result.new_state == state.value:
            return None
        return EnumReaperState(result.new_state)

    @staticmethod
    def _activity(
        obs: ModelReaperObservation,
        prior: ModelReaperTopicState | None,
        now: datetime,
    ) -> tuple[bool, datetime | None]:
        """Whether the broker shows activity now, and the newest activity time it names.

        A live member, or committed groups whose commit time was not read, count as activity at
        ``now`` (unknown means not quiet). Moved end offsets count at the newest record time, or ``now``.
        """
        stamps: list[datetime] = []
        live = False
        if obs.group_members:
            live = True
            stamps.append(now)
        if obs.committed_groups:
            if obs.last_commit_at is None:
                live = True
                stamps.append(now)
            else:
                stamps.append(obs.last_commit_at)
        if (
            prior is not None
            and prior.last_end_offset_total is not None
            and obs.end_offset_total is not None
            and obs.end_offset_total > prior.last_end_offset_total
        ):
            live = True
            stamps.append(obs.last_record_at or now)
        elif obs.last_record_at is not None:
            stamps.append(obs.last_record_at)
        return live, max(stamps) if stamps else None

    def handle(self, request: ModelTopicReaperRequest) -> ModelTopicReaperResult:
        policy = request.policy
        if policy is None:
            return ModelTopicReaperResult(
                status="policy_missing",
                report_code=REAPER_POLICY_MISSING,
                state=request.state,
            )
        now = request.now
        prior_by_topic = {record.topic: record for record in request.state}
        boot = frozenset(request.boot_set_topics)
        reannounced = frozenset(request.reannounced_topics)
        unfinished = frozenset(request.unfinished_work_topics)

        records: list[ModelReaperTopicState] = []
        decisions: list[ModelReaperDecision] = []
        targets: list[ModelReaperDeleteTarget] = []
        held_unread = 0
        deletable = 0
        deletable_partitions = 0
        without_id = 0
        seen: set[str] = set()

        for obs in sorted(request.observations, key=lambda o: o.topic):
            seen.add(obs.topic)
            prior = prior_by_topic.get(obs.topic)
            record, reason, tail = self._observe_topic(
                obs=obs,
                prior=prior,
                policy=policy,
                now=now,
                in_boot_set=obs.topic in boot,
                reannounced=obs.topic in reannounced,
                unfinished=obs.topic in unfinished,
            )
            if reason == "evidence_unread":
                held_unread += 1
            decisions.append(
                ModelReaperDecision(
                    topic=obs.topic,
                    prior_state=prior.state if prior else None,
                    state=record.state if record else None,
                    partitions=obs.partitions,
                    reason=reason,
                )
            )
            if record is None:
                continue
            if tail is not None:
                deletable += 1
                deletable_partitions += obs.partitions
                if record.topic_id is None:
                    without_id += 1
                elif policy.enforce:
                    targets.append(
                        ModelReaperDeleteTarget(
                            topic=obs.topic,
                            topic_id=record.topic_id,
                            partitions=obs.partitions,
                        )
                    )
            records.append(record)

        # A record whose topic the broker no longer lists is finished: a deleting one is confirmed
        # (through the machine), any other is simply gone. Neither is carried forward.
        for topic, prior in sorted(prior_by_topic.items()):
            if topic in seen:
                continue
            moved = (
                self._fire(prior.state, TRIGGER_CONFIRM, {})
                if prior.state is EnumReaperState.DELETING
                else None
            )
            decisions.append(
                ModelReaperDecision(
                    topic=topic,
                    prior_state=prior.state,
                    state=moved,
                    partitions=0,
                    reason="deleted_confirmed" if moved else "topic_gone",
                )
            )

        return ModelTopicReaperResult(
            status="observed",
            state=tuple(records),
            decisions=tuple(decisions),
            delete_targets=tuple(targets),
            flagged_topics=sum(
                1 for r in records if r.state is EnumReaperState.FLAGGED
            ),
            deletable_topics=deletable,
            deletable_partitions=deletable_partitions,
            deletable_without_topic_id=without_id,
            held_unread=held_unread,
        )

    def _observe_topic(
        self,
        *,
        obs: ModelReaperObservation,
        prior: ModelReaperTopicState | None,
        policy: ModelTopicReaperPolicy,
        now: datetime,
        in_boot_set: bool,
        reannounced: bool,
        unfinished: bool,
    ) -> tuple[ModelReaperTopicState | None, str, bool | None]:
        """One topic's new record (``None`` for an unread, never-seen topic), the reason it stands there,
        and ``True`` when it is deletable now."""
        state = prior.state if prior else EnumReaperState.ACTIVE
        topic_id = obs.topic_id or (prior.topic_id if prior else None)

        def record(
            new_state: EnumReaperState,
            *,
            activity_at: datetime,
            flagged_at: datetime | None,
            quiet: int,
        ) -> ModelReaperTopicState:
            return ModelReaperTopicState(
                topic=obs.topic,
                state=new_state,
                last_activity_at=activity_at,
                last_end_offset_total=obs.end_offset_total
                if obs.end_offset_total is not None
                else (prior.last_end_offset_total if prior else None),
                flagged_at=flagged_at,
                quiet_observations=quiet,
                topic_id=topic_id,
            )

        if (
            obs.end_offset_total is None
            or obs.group_members is None
            or (obs.committed_groups is None)
        ):
            # Unread evidence never moves a topic: a missing reading is not a quiet one.
            return prior, "evidence_unread", None

        live, newest = self._activity(obs, prior, now)
        # A topic that holds records but whose newest record time was not read has an unknown age:
        # with no record of it, it counts as active now rather than as old as its creation.
        produced_age_unknown = obs.end_offset_total > 0 and obs.last_record_at is None
        activity_at = newest or (
            prior.last_activity_at
            if prior
            else (now if produced_age_unknown else (obs.created_at or now))
        )
        if prior is not None and prior.last_activity_at > activity_at:
            activity_at = prior.last_activity_at
        quiet_for = now - activity_at
        is_quiet = not live and quiet_for >= timedelta(
            days=policy.quiet_days_before_flag
        )
        protected = obs.topic in policy.baseline_topics or any(
            obs.topic.startswith(prefix) for prefix in policy.protected_prefixes
        )

        if protected or in_boot_set or reannounced:
            if state is not EnumReaperState.ACTIVE:
                moved = self._fire(state, TRIGGER_UNFLAG, {})
                if moved is not None:
                    return (
                        record(
                            moved, activity_at=activity_at, flagged_at=None, quiet=0
                        ),
                        "re_announced" if reannounced else "declared_by_boot_set",
                        None,
                    )
            reason = (
                "protected"
                if protected
                else "declared_by_boot_set"
                if in_boot_set
                else "re_announced"
            )
            return (
                record(
                    EnumReaperState.ACTIVE,
                    activity_at=activity_at,
                    flagged_at=None,
                    quiet=0,
                ),
                reason,
                None,
            )

        if state is EnumReaperState.DELETING:
            # Still listed after the delete was handed out: hand it out again.
            return (
                record(
                    state,
                    activity_at=activity_at,
                    flagged_at=prior.flagged_at if prior else None,
                    quiet=prior.quiet_observations if prior else 0,
                ),
                "delete_not_yet_confirmed",
                True,
            )

        if state is EnumReaperState.ACTIVE:
            if not is_quiet:
                return (
                    record(state, activity_at=activity_at, flagged_at=None, quiet=0),
                    "active_not_quiet",
                    None,
                )
            flagged = self._fire(state, TRIGGER_FLAG, {})
            if flagged is None:
                return (
                    record(state, activity_at=activity_at, flagged_at=None, quiet=0),
                    "flag_refused",
                    None,
                )
            return (
                record(flagged, activity_at=activity_at, flagged_at=now, quiet=1),
                "flagged",
                None,
            )

        assert prior is not None
        since_flag = live or (
            newest is not None
            and prior.flagged_at is not None
            and newest > prior.flagged_at
        )
        if state is EnumReaperState.ELIGIBLE and since_flag:
            back = self._fire(state, TRIGGER_ACTIVITY, {})
            return (
                record(
                    back or state,
                    activity_at=activity_at,
                    flagged_at=now,
                    quiet=0,
                ),
                "activity_seen",
                None,
            )
        if since_flag:
            # Activity while flagged restarts the delete clock; the flag itself stays.
            return (
                record(state, activity_at=activity_at, flagged_at=now, quiet=0),
                "activity_seen",
                None,
            )

        quiet_count = prior.quiet_observations + 1
        flagged_at = prior.flagged_at or now
        if state is EnumReaperState.FLAGGED:
            elapsed = (now - flagged_at) >= timedelta(days=policy.flag_to_delete_days)
            interval_elapsed = (
                elapsed and quiet_count >= policy.quiet_observations_required
            )
            aged = self._fire(
                state, TRIGGER_AGE_OUT, {"interval_elapsed": interval_elapsed}
            )
            if aged is None:
                return (
                    record(
                        state,
                        activity_at=activity_at,
                        flagged_at=flagged_at,
                        quiet=quiet_count,
                    ),
                    "flag_standing",
                    None,
                )
            state = aged
        # eligible: a delete is handed out unless unfinished work still names the topic.
        issued = self._fire(
            state, TRIGGER_DELETE, {"unfinished_work_references": unfinished}
        )
        if issued is None:
            return (
                record(
                    state,
                    activity_at=activity_at,
                    flagged_at=flagged_at,
                    quiet=quiet_count,
                ),
                "unfinished_work_reference" if unfinished else "eligible",
                None if unfinished else True,
            )
        # In a report-only lane the record stays eligible: nothing was handed out.
        final = issued if policy.enforce else state
        return (
            record(
                final, activity_at=activity_at, flagged_at=flagged_at, quiet=quiet_count
            ),
            "deletable",
            True,
        )


__all__ = ["HandlerTopicReaper", "load_fsm"]
