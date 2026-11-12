# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Delete the long-Empty per-call consumer groups a reducer named, under the execution overlay (OMN-23992).

A command proves it was decided by the reducer through a recomputed decision digest; a command decided
from a file (decided_by not "reducer") is refused and deletes nothing. The reap POLICY (the empty-since
bound, the patterns, the per-decision cap) is read from the reducer's overlay; this node's own overlay
holds only the execution settings. Nothing is deleted on any refusal; a failure is a typed status, never
an escaped exception.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable, Mapping
from typing import Literal

from omnimarket.models.consumer_group_state import decision_digest
from omnimarket.nodes.node_consumer_group_reap_effect.adapters.kafka_group_admin import (
    KafkaGroupAdmin,
)
from omnimarket.nodes.node_consumer_group_reap_effect.models.model_group_reap import (
    EnumReapRefusal,
    ModelGroupReapCommand,
    ModelGroupReapOutcome,
    ModelGroupReapRefusal,
)
from omnimarket.nodes.node_consumer_group_reap_effect.overlay import (
    ModelGroupReapOverlay,
    load_overlay,
)
from omnimarket.nodes.node_consumer_group_reap_effect.protocols import (
    BrokerHealth,
    GroupDeleter,
    GroupLister,
)
from omnimarket.nodes.node_consumer_group_state_reducer.overlay import (
    ModelGroupStateOverlay,
)
from omnimarket.nodes.node_consumer_group_state_reducer.overlay import (
    load_overlay as load_policy_overlay,
)

# Outcomes that mean the group changed under us rather than that the broker is unwell.
_BENIGN_ERRORS = ("GROUP_ID_NOT_FOUND", "NON_EMPTY_GROUP")


BrokerResolver = Callable[[str], tuple[object, str, Mapping[str, str]]]


def _default_resolve_broker(lane: str) -> tuple[object, str, Mapping[str, str]]:
    from omnimarket.nodes.node_runtime_coverage_effect.handlers.handler_runtime_coverage import (
        resolve_broker,
    )

    return resolve_broker(lane)


class HandlerGroupReap:
    """Delete the due groups a reducer named, only on a command that proves the reducer decided them."""

    def __init__(
        self,
        *,
        policy: ModelGroupStateOverlay | None = None,
        overlay: ModelGroupReapOverlay | None = None,
        lister: GroupLister | None = None,
        deleter: GroupDeleter | None = None,
        health: BrokerHealth | None = None,
        sleeper: Callable[[float], None] | None = None,
        resolve_broker: BrokerResolver | None = None,
    ) -> None:
        # Every argument is optional so the runtime can build the handler with no arguments; an
        # absent one resolves on first use, so a missing overlay surfaces as a typed "failed"
        # outcome of the run, never as a crash at wiring time.
        self._policy_arg = policy
        self._overlay_arg = overlay
        self._lister_arg = lister
        self._deleter_arg = deleter
        self._health_arg = health
        self._sleeper_arg = sleeper
        self._resolve_broker_arg = resolve_broker

    @property
    def _policy(self) -> ModelGroupStateOverlay:
        return self._policy_arg or load_policy_overlay()

    @property
    def _overlay(self) -> ModelGroupReapOverlay:
        return self._overlay_arg or load_overlay()

    def _admin(self) -> KafkaGroupAdmin:
        return KafkaGroupAdmin(self._overlay.request_timeout_seconds)

    def _lister(self, config: Mapping[str, str]) -> dict[str, str]:
        return (self._lister_arg or self._admin().list_groups)(config)

    def _deleter(self, config: Mapping[str, str], names: list[str]) -> dict[str, str]:
        return (self._deleter_arg or self._admin().delete_groups)(config, names)

    def _health(self, config: Mapping[str, str]) -> str:
        return (self._health_arg or self._admin().check_health)(config)

    def _sleeper(self, seconds: float) -> None:
        (self._sleeper_arg or time.sleep)(seconds)

    def _resolve_broker(self, lane: str) -> tuple[object, str, Mapping[str, str]]:
        return (self._resolve_broker_arg or _default_resolve_broker)(lane)

    def _decided_by_reducer(self, command: ModelGroupReapCommand) -> bool:
        """The single check that a command came from a reducer; a file-decided command is refused."""
        return command.decided_by == "reducer"

    async def handle(self, request: ModelGroupReapCommand) -> ModelGroupReapOutcome:
        try:
            return await asyncio.to_thread(self._run, request)
        except Exception as exc:
            return ModelGroupReapOutcome(
                correlation_id=request.correlation_id,
                bus_lane=request.bus_lane,
                status="failed",
                detail=f"reap failed: {type(exc).__name__}",
            )

    def _run(self, command: ModelGroupReapCommand) -> ModelGroupReapOutcome:
        base = {
            "correlation_id": command.correlation_id,
            "bus_lane": command.bus_lane,
        }
        refusals: list[ModelGroupReapRefusal] = []

        # 1. the execution overlay disables the effect entirely.
        if not self._overlay.enabled:
            refusals.append(
                ModelGroupReapRefusal(
                    code=EnumReapRefusal.DISABLED,
                    detail="reaping disabled by the execution overlay",
                )
            )
        # 2. a command decided from a file (or anything but the reducer) is refused and deletes nothing.
        if not self._decided_by_reducer(command):
            refusals.append(
                ModelGroupReapRefusal(
                    code=EnumReapRefusal.DECIDED_NOT_BY_REDUCER,
                    detail=f"decided_by is {command.decided_by!r}, not the reducer",
                )
            )
        # 3. recompute the digest; a mismatch means the command does not prove a reducer decision.
        empty_since_by_name = {g.name: g.empty_since for g in command.groups}
        recomputed = decision_digest(
            command.bus_lane,
            command.census_correlation_id,
            tuple(g.name for g in command.groups),
            empty_since_by_name,
            command.observed_at,
        )
        if recomputed != command.decision_digest:
            refusals.append(
                ModelGroupReapRefusal(
                    code=EnumReapRefusal.DIGEST_MISMATCH,
                    detail="the recomputed decision digest does not match the command",
                )
            )
        # 4. the command names more groups than one run may delete.
        if len(command.groups) > self._overlay.max_deletes_per_run:
            refusals.append(
                ModelGroupReapRefusal(
                    code=EnumReapRefusal.OVER_CAP,
                    detail=f"{len(command.groups)} groups exceed the per-run cap of {self._overlay.max_deletes_per_run}",
                )
            )
        # 5. per-group policy checks: protected, not reapable, and the empty-since bound.
        protected = [re.compile(p) for p in self._policy.protected_patterns]
        reapable = [re.compile(p) for p in self._policy.reapable_patterns]
        for group in command.groups:
            if any(p.search(group.name) for p in protected):
                refusals.append(
                    ModelGroupReapRefusal(
                        code=EnumReapRefusal.PROTECTED,
                        group=group.name,
                        detail="the name matches a protected pattern",
                    )
                )
                continue
            if not any(p.search(group.name) for p in reapable):
                refusals.append(
                    ModelGroupReapRefusal(
                        code=EnumReapRefusal.NOT_REAPABLE,
                        group=group.name,
                        detail="the name matches no reapable pattern",
                    )
                )
                continue
            if (
                command.observed_at - group.empty_since
            ).total_seconds() < self._policy.empty_since_bound_seconds:
                refusals.append(
                    ModelGroupReapRefusal(
                        code=EnumReapRefusal.BOUND_NOT_MET,
                        group=group.name,
                        detail="the group has not been empty long enough",
                    )
                )

        if refusals:
            return ModelGroupReapOutcome(
                **base,
                status="refused",
                refusals=tuple(refusals),
                detail="the command was refused and nothing was deleted",
            )

        _broker, _environment, config = self._resolve_broker(command.bus_lane)

        # 6. list the groups live; a due group that is not listed, or is not EMPTY now, is dropped silently.
        states = self._lister(config)
        dropped: list[str] = []
        to_delete: list[str] = []
        for group in command.groups:
            state = states.get(group.name)
            if state is None or state != "EMPTY":
                dropped.append(group.name)
            else:
                to_delete.append(group.name)
        detail = (
            f"dropped {len(dropped)} group(s) that are no longer listed or no longer EMPTY"
            if dropped
            else ""
        )

        # 7. a dry run lists what it would delete and touches nothing.
        if self._overlay.dry_run:
            return ModelGroupReapOutcome(
                **base,
                status="dry_run",
                would_delete=tuple(to_delete),
                detail=detail,
            )

        # 8. the broker must be healthy before the first batch.
        reason = self._health(config)
        if reason:
            return ModelGroupReapOutcome(
                **base,
                status="refused",
                refusals=(
                    ModelGroupReapRefusal(
                        code=EnumReapRefusal.BROKER_UNHEALTHY,
                        detail=f"the broker is not healthy before the first batch: {reason}",
                    ),
                ),
                detail=detail,
            )

        deleted: list[str] = []
        delete_errors: dict[str, str] = {}
        stop_reason = ""
        for start in range(0, len(to_delete), self._overlay.batch_size):
            batch = to_delete[start : start + self._overlay.batch_size]
            outcome = self._deleter(config, batch)
            for name, error in outcome.items():
                if error == "":
                    deleted.append(name)
                elif error in _BENIGN_ERRORS:
                    continue
                else:
                    delete_errors[name] = error
            errors = [e for e in outcome.values() if e not in ("", *_BENIGN_ERRORS)]
            if errors:
                stop_reason = f"stopped at the first delete error: {', '.join(sorted(set(errors)))}"
                break
            reason = self._health(config)
            if reason:
                stop_reason = (
                    f"stopped, the broker is not healthy after a batch: {reason}"
                )
                break

        # 9. read back: wait, list again, up to the attempts, until none of the deleted names is listed.
        read_back_absent: list[str] = []
        read_back_present: list[str] = []
        if deleted:
            for _ in range(self._overlay.read_back_attempts):
                if self._overlay.read_back_wait_seconds:
                    self._sleeper(self._overlay.read_back_wait_seconds)
                listed = self._lister(config)
                read_back_present = [n for n in deleted if n in listed]
                read_back_absent = [n for n in deleted if n not in listed]
                if not read_back_present:
                    break

        status: Literal["reaped", "partial", "failed"]
        if stop_reason:
            status = "failed" if delete_errors else "partial"
            detail = f"{detail} {stop_reason}".strip()
        elif read_back_present:
            status = "partial"
            detail = (
                f"{detail} read-back still lists {', '.join(read_back_present)} after the last attempt"
            ).strip()
        else:
            status = "reaped"
            detail = f"{detail} reaped {len(deleted)} group(s)".strip()

        return ModelGroupReapOutcome(
            **base,
            status=status,
            deleted=tuple(deleted),
            delete_errors=delete_errors,
            read_back_absent=tuple(read_back_absent),
            read_back_present=tuple(read_back_present),
            detail=detail,
        )


__all__: list[str] = ["HandlerGroupReap"]
