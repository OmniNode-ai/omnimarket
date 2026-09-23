# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Projection writer for per-attempt continuous-integration outcomes.

OMN-18903. The effect-class half of the rule-7a pair: this is the entry the
runtime actually calls, and it holds no business logic beyond calling the fold
and persisting its result. The derivation is imported from
``HandlerProjectionCiAttemptOutcome`` rather than duplicated, so the writer and
the reducer cannot drift into disagreeing about what a row means.

Two ways to get this shape wrong produce the SAME silent symptom -- every
message consumed, offsets committed, zero rows, no error. A pure definition-B
entry on this arm validates and returns without writing. A runner-shaped class
that does not declare in-process dispatch is skipped by the shared runtime
entirely. Both are pinned by tests.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import yaml

from omnimarket.nodes.node_projection_ci_attempt_outcome.handlers.handler_projection_ci_attempt_outcome import (
    HandlerProjectionCiAttemptOutcome,
)
from omnimarket.nodes.node_projection_ci_attempt_outcome.models import (
    ModelCiAttemptOutcomeProjectionRequest,
    ModelCiAttemptOutcomeRow,
)
from omnimarket.projection.runner import BaseProjectionRunner, MessageMeta

TABLE_CI_ATTEMPT_OUTCOME = "omninode_internal.ci_attempt_outcome"

# The upsert is keyed on the full five-column grain. The conflict arm exists
# because the inventory sweep is idempotent and re-reports the same attempt on
# every run; a re-report must correct the row rather than duplicate it or
# fail.
#
# The stale-write guard is in the WHERE clause rather than in a read-then-write
# around it. A read-compare-write races under concurrent consumers and lets a
# redelivered older observation win, and the failure is invisible because both
# writes succeed.
_UPSERT_ATTEMPT = f"""
    INSERT INTO {TABLE_CI_ATTEMPT_OUTCOME} (
        repository, pr_number, head_sha, check_name, run_attempt,
        cause_code, cause_affirmative, attempt_ordinal,
        ticket_id, failed_step_name, run_id, job_conclusion,
        observed_at, first_seen_at, updated_at
    )
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, NOW(), NOW())
    ON CONFLICT (repository, pr_number, head_sha, check_name, run_attempt)
    DO UPDATE SET
        cause_code = EXCLUDED.cause_code,
        cause_affirmative = EXCLUDED.cause_affirmative,
        attempt_ordinal = EXCLUDED.attempt_ordinal,
        ticket_id = EXCLUDED.ticket_id,
        failed_step_name = EXCLUDED.failed_step_name,
        run_id = EXCLUDED.run_id,
        job_conclusion = EXCLUDED.job_conclusion,
        observed_at = EXCLUDED.observed_at,
        updated_at = NOW()
    WHERE {TABLE_CI_ATTEMPT_OUTCOME}.observed_at < EXCLUDED.observed_at
    RETURNING repository, pr_number, head_sha, check_name, run_attempt,
              cause_code, cause_affirmative, attempt_ordinal, ticket_id,
              observed_at, projection_cursor
"""


def _row_to_wire(
    row: ModelCiAttemptOutcomeRow, extra: dict[str, Any]
) -> dict[str, Any]:
    """Merge the derived row with the database-assigned columns for publish."""
    payload: dict[str, Any] = {
        "repository": row.repository,
        "pr_number": row.pr_number,
        "head_sha": row.head_sha,
        "check_name": row.check_name,
        "run_attempt": row.run_attempt,
        "cause_code": row.cause_code.value,
        "cause_affirmative": row.cause_affirmative,
        "cause_class": row.cause_class.value,
        "attempt_ordinal": row.attempt_ordinal,
        "ticket_id": row.ticket_id,
        "failed_step_name": row.failed_step_name,
        "run_id": row.run_id,
        "job_conclusion": row.job_conclusion,
        "observed_at": row.observed_at,
    }
    payload.update(extra)
    return payload


class CiAttemptOutcomeProjectionWriter(BaseProjectionRunner):
    """Projects classified check outcomes into ``ci_attempt_outcome``.

    Named ``Writer`` rather than ``Runner``: the OMN-14350 type-word ratchet
    hard-fails ``Runner`` in a class name and its allowlist may only shrink.
    ``Writer`` is also the accurate word for the role the deployed
    ``*-writer`` services carry.
    """

    #: Dispatched IN-PROCESS by the runtime auto-wiring, once per consumed
    #: message, rather than driven by its own consume loop. Declaring it is a
    #: promise: ``handle()`` opens one event loop per message, so every
    #: loop-bound resource this class touches is opened and closed inside that
    #: loop. A pool cached across calls belongs to a loop that no longer
    #: exists, and reaching for it raises "Event loop is closed".
    #:
    #: Without the declaration the shared runtime treats this as a STANDALONE
    #: runner and never dispatches it. There is no dedicated writer Deployment
    #: for this node, so undeclared means dispatched by nobody -- zero rows,
    #: no error, every watermark healthy.
    onex_runtime_inprocess_dispatch = True

    def __init__(self, contract_path: Path | None = None) -> None:
        super().__init__()
        _path = contract_path or Path(__file__).parent.parent / "contract.yaml"
        with open(_path) as handle:
            self._contract: dict[str, Any] = yaml.safe_load(handle)
        self._derive = HandlerProjectionCiAttemptOutcome()

    @property
    def subscribe_topics(self) -> list[str]:
        return list(self._contract.get("event_bus", {}).get("subscribe_topics", []))

    @property
    def topics(self) -> list[str]:
        return self.subscribe_topics

    def handle(self, input_data: dict[str, Any]) -> dict[str, Any]:
        """RuntimeLocal handler protocol shim: one message, one loop, one pool."""
        topics = self.subscribe_topics
        topic = str(input_data.pop("_topic", topics[0] if topics else ""))
        meta = MessageMeta(
            partition=int(input_data.pop("_partition", 0)),
            offset=int(input_data.pop("_offset", 0)),
            fallback_id=str(input_data.pop("_fallback_id", "")),
            topic=topic,
        )
        return asyncio.run(self._project_one_message(topic, input_data, meta))

    async def _project_one_message(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> dict[str, Any]:
        """Project one runtime-dispatched message and report what changed.

        The returned mapping carries a row COUNT, which the runtime's
        write-path guard reads. A bare truthy acknowledgement over a message
        that wrote nothing is indistinguishable from one that wrote a hundred
        rows, and telling those apart is the whole reason this projection
        exists.
        """
        await self.db.connect()
        try:
            written, unattributed, skipped = await self._project_event(data, meta)
        finally:
            await self.db.close()
        return {
            "rows_written": len(written),
            "attempt_rows": written,
            "unattributed_row_count": unattributed,
            "skipped_check_count": skipped,
        }

    async def project_event(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        """Standalone-runner entrypoint: project one message, report success."""
        await self._project_event(data, meta)
        return True

    async def _project_event(
        self, data: dict[str, Any], meta: MessageMeta
    ) -> tuple[list[dict[str, Any]], int, int]:
        request = ModelCiAttemptOutcomeProjectionRequest.model_validate(data)
        result = self._derive.handle(request)

        written: list[dict[str, Any]] = []
        for row in result.rows:
            returned = await self.db.execute(
                _UPSERT_ATTEMPT,
                row.repository,
                row.pr_number,
                row.head_sha,
                row.check_name,
                row.run_attempt,
                row.cause_code.value,
                row.cause_affirmative,
                row.attempt_ordinal,
                row.ticket_id,
                row.failed_step_name,
                row.run_id,
                row.job_conclusion,
                row.observed_at,
            )
            if not returned:
                # The stale-write guard refused it: a redelivered event older
                # than what is stored. Not an error, and deliberately not
                # counted as written.
                continue
            written.append(_row_to_wire(row, dict(returned[0])))

        return written, result.unattributed_row_count, result.skipped_check_count


__all__ = ["CiAttemptOutcomeProjectionWriter"]
