# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Consume lab submissions at the delegation effect's typed handler boundary.

The database primary key arbitrates the take before any agent starts. Reducer
rows and transition publications are committed together, then drained from an
outbox. A redelivery drains retained publications and never starts a second
session. Recovery of a dead owner belongs to the job supervisor.
"""

from __future__ import annotations

import json
import socket
import uuid
from datetime import UTC, datetime
from importlib import resources
from typing import Any, Literal, Protocol

import yaml

from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter
from omnimarket.delegated_test_loop.lab_run_bus import (
    ProtocolLabRunBus,
    envelope_bytes_for,
)
from omnimarket.inference.secret_store_resolver import resolve_api_key_async
from omnimarket.models.delegation.model_delegation_target import (
    ModelDelegationTargetRequest,
    ModelDelegationTargetResult,
)
from omnimarket.models.lab_job import (
    EnumLabJobAttemptOutcome,
    EnumLabJobDoneCriterionKind,
    EnumLabJobEngine,
    EnumLabJobEventKind,
    ModelLabJobEvent,
    ModelLabJobReduceInput,
    ModelLabJobRow,
    ModelLabJobSpec,
    ModelLabJobTransitioned,
)
from omnimarket.nodes.node_lab_job_reducer import HandlerLabJobReducer
from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_delegation_target import (
    HandlerDelegationTarget,
    load_delegation_config,
)


def lab_job_topics() -> dict[str, str]:
    """Both command and publication addresses are owned by this contract."""
    contract = yaml.safe_load(
        resources.files("omnimarket.nodes.node_llm_delegation_call_effect")
        .joinpath("contract.yaml")
        .read_text()
    )
    return dict(contract["lab_job_dispatch"])


class ProtocolLabJobDelegationStore(Protocol):
    async def claim(self, spec: ModelLabJobSpec, runtime_id: str) -> bool: ...
    async def append(self, value: ModelLabJobTransitioned) -> None: ...
    async def finish(
        self, job_id: str, result: ModelDelegationTargetResult
    ) -> None: ...
    async def flush(self, bus: ProtocolLabRunBus) -> None: ...


class PostgresLabJobDelegationStore:
    """Durable claims and ordered outbox on the configured runtime database."""

    def __init__(self, database: AsyncpgAdapter, job_id: str) -> None:
        self._db, self._job_id = database, job_id

    async def claim(self, spec: ModelLabJobSpec, runtime_id: str) -> bool:
        async with self._db.pool.acquire() as conn, conn.transaction():
            inserted = await conn.fetchval(
                """INSERT INTO omninode_internal.delegation_lab_jobs
                       (job_id, spec, owner_runtime)
                   VALUES ($1, $2::jsonb, $3)
                   ON CONFLICT (job_id) DO NOTHING RETURNING job_id""",
                spec.job_id,
                spec.model_dump_json(),
                runtime_id,
            )
            if inserted is None:
                saved = await conn.fetchval(
                    "SELECT spec FROM omninode_internal.delegation_lab_jobs WHERE job_id=$1",
                    spec.job_id,
                )
                original = json.loads(saved) if isinstance(saved, str) else saved
                if original != spec.model_dump(mode="json"):
                    raise ValueError(
                        "job_id already names a different recorded brief/spec"
                    )
        return inserted is not None

    async def append(self, value: ModelLabJobTransitioned) -> None:
        async with self._db.pool.acquire() as conn, conn.transaction():
            changed = await conn.fetchval(
                """UPDATE omninode_internal.delegation_lab_jobs
                      SET seq=$2, row_data=$3::jsonb
                    WHERE job_id=$1 AND seq=$2-1 RETURNING job_id""",
                value.row.job_id,
                value.row.seq,
                value.row.model_dump_json(),
            )
            if changed is None:
                raise RuntimeError("lab job transition compare-and-set refused")
            await conn.execute(
                """INSERT INTO omninode_internal.delegation_lab_job_outbox
                       (job_id, seq, payload) VALUES ($1, $2, $3::jsonb)""",
                value.row.job_id,
                value.row.seq,
                value.model_dump_json(),
            )

    async def finish(self, job_id: str, result: ModelDelegationTargetResult) -> None:
        async with self._db.pool.acquire() as conn:
            await conn.execute(
                "UPDATE omninode_internal.delegation_lab_jobs SET result=$2::jsonb WHERE job_id=$1",
                job_id,
                result.model_dump_json(),
            )

    async def flush(self, bus: ProtocolLabRunBus) -> None:
        topic = lab_job_topics()["transition_topic"]
        async with self._db.pool.acquire() as conn:
            pending = await conn.fetch(
                """SELECT seq, payload FROM omninode_internal.delegation_lab_job_outbox
                    WHERE job_id=$1 AND NOT published ORDER BY seq""",
                self._job_id,
            )
            for record in pending:
                payload = record["payload"]
                if isinstance(payload, str):
                    payload = json.loads(payload)
                await bus.publish(
                    topic,
                    self._job_id.encode(),
                    envelope_bytes_for(
                        topic,
                        payload,
                        uuid.uuid5(uuid.NAMESPACE_URL, self._job_id),
                    ),
                )
                await conn.execute(
                    """UPDATE omninode_internal.delegation_lab_job_outbox SET published=TRUE
                        WHERE job_id=$1 AND seq=$2""",
                    self._job_id,
                    record["seq"],
                )


class HandlerLabJobDelegation:
    """One submitted job -> durable take -> agent adapter -> reducer transitions."""

    def __init__(
        self,
        event_bus: ProtocolLabRunBus,
        *,
        store: ProtocolLabJobDelegationStore | None = None,
        target_handler: Any = None,
        runtime_id: str | None = None,
    ) -> None:
        self._bus = event_bus
        self._store = store
        self._target = target_handler
        self._runtime_id = runtime_id or socket.gethostname().split(".", 1)[0]
        self._reducer = HandlerLabJobReducer()

    @property
    def handler_type(self) -> Literal["NODE_HANDLER"]:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> Literal["EFFECT"]:
        return "EFFECT"

    async def handle(self, spec: ModelLabJobSpec) -> ModelDelegationTargetResult:
        config = load_delegation_config() if self._store is None else {}
        runtime_id = config.get("runtime_id", self._runtime_id)
        if self._bus is None:
            raise RuntimeError("lab job delegation requires the runtime event_bus")
        if spec.host_preference and runtime_id not in spec.host_preference:
            raise ValueError("job's host preference excludes this runtime host")
        targets = {
            EnumLabJobEngine.CODEX: "codex",
            EnumLabJobEngine.OPUS: "claude-code",
            EnumLabJobEngine.SONNET: "claude-code",
            EnumLabJobEngine.GLM: "claude-glm",
        }
        if spec.engine not in targets:
            raise ValueError("lab job engine has no coding-session adapter")
        database: AsyncpgAdapter | None = None
        store = self._store
        target = self._target
        if store is None:
            # An unset database reference is a refusal, never a fallback to a
            # dashboard login or an in-memory claim on a public command topic.
            ref = config.get("database_secret_ref")
            if not ref:
                raise RuntimeError("delegation database_secret_ref is not configured")
            secret = await resolve_api_key_async(ref, required=True)
            if secret is None:
                raise RuntimeError("delegation database reference did not resolve")
            database = AsyncpgAdapter(
                dsn=secret.get_secret_value(), min_size=1, max_size=2
            )
            await database.connect()
            store = PostgresLabJobDelegationStore(database, spec.job_id)
            target = target or HandlerDelegationTarget(
                config=config, event_bus=self._bus
            )
        target = target or HandlerDelegationTarget(event_bus=self._bus)
        try:
            return await self._execute(
                spec, targets[spec.engine], store, target, runtime_id
            )
        finally:
            if database is not None:
                await database.close()

    async def _execute(
        self,
        spec: ModelLabJobSpec,
        target_name: str,
        store: ProtocolLabJobDelegationStore,
        target: Any,
        runtime_id: str,
    ) -> ModelDelegationTargetResult:
        request_id = uuid.uuid5(uuid.NAMESPACE_URL, f"{spec.job_id}:attempt:1")
        if not await store.claim(spec, runtime_id):
            await store.flush(self._bus)
            return ModelDelegationTargetResult(
                request_id=request_id, target=target_name, status="duplicate"
            )
        row: ModelLabJobRow | None = None

        async def reduce(kind: EnumLabJobEventKind, **values: Any) -> None:
            nonlocal row
            reduced = self._reducer.handle(
                ModelLabJobReduceInput(
                    row=row,
                    event=ModelLabJobEvent(
                        kind=kind, job_id=spec.job_id, at=datetime.now(UTC), **values
                    ),
                )
            )
            if not reduced.applied or reduced.row is None:
                raise RuntimeError(
                    f"lab job reducer refused {kind}: {reduced.drop_reason}"
                )
            row = reduced.row
            for transition in reduced.transitions:
                intermediate = row.model_copy(
                    update={
                        "seq": transition.seq,
                        "state": transition.to_state,
                        "entered_state_at": transition.at,
                    }
                )
                await store.append(
                    ModelLabJobTransitioned(row=intermediate, transition=transition)
                )
            await store.flush(self._bus)

        await reduce(EnumLabJobEventKind.SUBMITTED, spec=spec)
        await reduce(EnumLabJobEventKind.TAKEN, attempt=1, runtime_id=runtime_id)
        await reduce(EnumLabJobEventKind.CLAIMED, attempt=1, run_id=str(request_id))
        result: ModelDelegationTargetResult
        try:
            result = await target.handle(
                ModelDelegationTargetRequest(
                    request_id=request_id,
                    target=target_name,
                    brief=spec.brief,
                    repo=spec.repo,
                    commit_sha=spec.ref,
                    call_site="lab_job",
                    timeout_seconds=spec.time_box_min * 60,
                    agent_profile=spec.engine.value,
                )
            )
        except Exception as exc:
            # Provider/credential diagnostics can contain values. The result
            # names only the exception class and omits original exception text.
            result = ModelDelegationTargetResult(
                request_id=request_id,
                target=target_name,
                status="failed",
                detail=f"{type(exc).__name__}: delegation adapter failed",
                commit_sha=spec.ref,
            )
        await store.finish(spec.job_id, result)
        outcome = (
            EnumLabJobAttemptOutcome.TIMED_OUT
            if result.status == "timed_out"
            else EnumLabJobAttemptOutcome.EXITED_ZERO
            if result.status == "completed" and result.exit_code == 0
            else EnumLabJobAttemptOutcome.EXITED_NONZERO
        )
        await reduce(EnumLabJobEventKind.ATTEMPT_ENDED, attempt=1, outcome=outcome)
        # A process exit does not establish a PR or ledger-based done rule.
        # Those stay CHECKING for the existing evidence checker/supervisor.
        if all(
            criterion.kind is EnumLabJobDoneCriterionKind.EXIT_ZERO
            for criterion in spec.done_criteria
        ):
            await reduce(
                EnumLabJobEventKind.DONE_EVALUATED,
                attempt=1,
                done_rule_met=outcome is EnumLabJobAttemptOutcome.EXITED_ZERO,
            )
        return result


__all__ = [
    "HandlerLabJobDelegation",
    "PostgresLabJobDelegationStore",
    "ProtocolLabJobDelegationStore",
]
