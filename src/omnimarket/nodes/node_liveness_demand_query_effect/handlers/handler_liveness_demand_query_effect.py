# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerLivenessDemandQueryEffect — real Postgres demand + join query (OMN-15126).

Implements design §3.2 steps 2-3 as real I/O against a `table_query` demand
source:

1. Query `demand_source.locator` filtered by `demand_source.eligibility_predicate`
   for eligible demand rows (an input-event correlation_id per row).
2. For every eligible row, query `expected_output_join.projection_table` for a
   row with the same `correlation_id` that also satisfies
   `expected_output_join.expected_value_predicate` -- this is the exact
   input-event-to-terminal-event-to-projection-key/value join the design
   requires (design §1), never a bare count.

Scope (v1, honest and fail-closed rather than silently wrong):

- Only `demand_source.kind == "table_query"` is implemented. `kafka_topic` /
  `scheduled_trigger` / `webhook` demand sources return `query_succeeded=False`
  (design §3.2 step-2 NOT_READY case), not a silent no-op.
- Only `expected_output_join.projection_key_fields == ("correlation_id",)` and
  `projection_key_canonicalization == "json_sorted_keys"` are implemented,
  matching the join semantics used by every OmniNode event-ledger-shaped
  table. Any other declaration fails closed with an explicit error_message.
- A registry entry with a non-None `sampling_policy` fails closed (design §4
  OPEN-8 is unresolved) rather than silently sampling and under-checking.
- `demand_source.locator` / `expected_output_join.projection_table` are
  validated as bare SQL identifiers before interpolation (they cannot be
  parameterized as table names via asyncpg). `eligibility_predicate` /
  `expected_value_predicate` are operator-authored registry config (design
  §4), not end-user input, and are interpolated directly as trusted SQL
  boolean expressions -- exactly the design's own contract for these fields
  -- with a narrow guard against statement-terminating/comment markers as a
  defence against operator copy-paste error, not a substitute for treating
  the registry catalog as a trusted surface.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from typing import TYPE_CHECKING, Any, Literal

from omnibase_core.models.runtime.model_event_ref import ModelEventRef

from omnimarket.nodes.node_liveness_demand_query_effect.models.model_liveness_demand_query_request import (
    ModelLivenessDemandQueryRequest,
)
from omnimarket.nodes.node_liveness_demand_query_effect.models.model_liveness_demand_query_result import (
    ModelLivenessDemandQueryResult,
)
from omnimarket.nodes.node_liveness_demand_query_effect.models.model_liveness_join_sample import (
    ModelLivenessJoinSample,
)

if TYPE_CHECKING:
    import asyncpg

logger = logging.getLogger(__name__)

_DEFAULT_PG_DSN = os.environ.get("ONEX_PG_DSN", "")  # contract-config-ok: config  # fmt: skip

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?$")
_FORBIDDEN_PREDICATE_MARKERS = (";", "--", "/*")
_SUPPORTED_KEY_FIELDS = ("correlation_id",)
_SUPPORTED_KEY_CANONICALIZATION = "json_sorted_keys"


def _validate_identifier(value: str, *, field: str) -> None:
    if not _IDENTIFIER_RE.match(value):
        raise ValueError(
            f"{field}={value!r} is not a safe SQL identifier (letters/digits/"
            "underscore, optional single dot-qualifier only)."
        )


def _validate_predicate(value: str, *, field: str) -> None:
    for marker in _FORBIDDEN_PREDICATE_MARKERS:
        if marker in value:
            raise ValueError(f"{field} contains forbidden marker {marker!r}: {value!r}")


def _sha256_json(obj: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, default=str).encode()
    ).hexdigest()


class HandlerLivenessDemandQueryEffect:
    """EFFECT handler: real Postgres demand-source query + correlated join."""

    @property
    def handler_type(self) -> Literal["NODE_HANDLER"]:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> Literal["EFFECT"]:
        return "EFFECT"

    def __init__(
        self,
        pool: asyncpg.Pool | None = None,
        pg_dsn: str = _DEFAULT_PG_DSN,
    ) -> None:
        self._injected_pool = pool
        self._pg_dsn = pg_dsn
        self._pool: asyncpg.Pool | None = pool

    async def _get_pool(self) -> asyncpg.Pool | None:
        if self._pool is not None:
            return self._pool
        if not self._pg_dsn:
            return None
        import asyncpg as _asyncpg

        self._pool = await _asyncpg.create_pool(
            dsn=self._pg_dsn,
            min_size=1,
            max_size=5,
            command_timeout=10,
        )
        return self._pool

    @staticmethod
    def _not_ready(surface_id: str, message: str) -> ModelLivenessDemandQueryResult:
        return ModelLivenessDemandQueryResult(
            surface_id=surface_id,
            query_succeeded=False,
            error_message=message,
            eligible_count=0,
            checked_count=0,
            failed_count=0,
        )

    async def handle(
        self,
        request: ModelLivenessDemandQueryRequest,
    ) -> ModelLivenessDemandQueryResult:
        entry = request.registry_entry
        surface_id = entry.surface_id
        demand = entry.demand_source
        join = entry.expected_output_join

        if demand.kind != "table_query":
            return self._not_ready(
                surface_id,
                f"demand_source.kind={demand.kind!r} not supported by this v1 "
                "evaluator (only 'table_query' is implemented).",
            )
        if entry.sampling_policy is not None:
            return self._not_ready(
                surface_id,
                "registry_entry.sampling_policy is set but this v1 evaluator "
                "does not implement sampling (design §4 OPEN-8 unresolved) -- "
                "failing closed rather than silently checking every eligible "
                "item as if sampling_policy were None.",
            )
        if join.projection_key_fields != _SUPPORTED_KEY_FIELDS:
            return self._not_ready(
                surface_id,
                f"expected_output_join.projection_key_fields={join.projection_key_fields!r} "
                f"not supported (v1 only supports {_SUPPORTED_KEY_FIELDS!r}).",
            )
        if join.projection_key_canonicalization != _SUPPORTED_KEY_CANONICALIZATION:
            return self._not_ready(
                surface_id,
                "expected_output_join.projection_key_canonicalization="
                f"{join.projection_key_canonicalization!r} not supported "
                f"(v1 only supports {_SUPPORTED_KEY_CANONICALIZATION!r}).",
            )

        try:
            _validate_identifier(demand.locator, field="demand_source.locator")
            _validate_identifier(
                join.projection_table, field="expected_output_join.projection_table"
            )
            _validate_predicate(
                demand.eligibility_predicate,
                field="demand_source.eligibility_predicate",
            )
            _validate_predicate(
                join.expected_value_predicate,
                field="expected_output_join.expected_value_predicate",
            )
        except ValueError as exc:
            return self._not_ready(surface_id, str(exc))

        pool = await self._get_pool()
        if pool is None:
            return self._not_ready(
                surface_id,
                "no Postgres pool available (ONEX_PG_DSN unset and no pool injected)",
            )

        eligible_sql = (
            "SELECT ledger_entry_id, partition, kafka_offset, correlation_id "
            f"FROM {demand.locator} "
            f"WHERE ({demand.eligibility_predicate}) AND correlation_id IS NOT NULL "
            "ORDER BY ledger_written_at DESC LIMIT $1"
        )
        try:
            async with pool.acquire() as conn:
                rows = await conn.fetch(eligible_sql, request.evaluation_window_limit)
        except Exception as exc:
            logger.warning(
                "liveness_demand_query_effect: eligible-demand query failed "
                "for surface_id=%s: %s",
                surface_id,
                exc,
            )
            return self._not_ready(surface_id, f"demand query failed: {exc}")

        eligible_count = len(rows)
        query_evidence = (
            f"query_table={demand.locator} "
            f"eligibility_predicate={demand.eligibility_predicate!r} "
            f"row_count={eligible_count}"
        )

        if eligible_count == 0:
            return ModelLivenessDemandQueryResult(
                surface_id=surface_id,
                query_succeeded=True,
                eligible_count=0,
                checked_count=0,
                failed_count=0,
                demand_query_evidence=query_evidence,
            )

        join_sql = (
            "SELECT ledger_entry_id, partition, kafka_offset "
            f"FROM {join.projection_table} "
            f"WHERE correlation_id = $1 AND ({join.expected_value_predicate}) "
            "ORDER BY ledger_written_at DESC LIMIT 1"
        )

        checked = 0
        failed = 0
        healthy_sample: ModelLivenessJoinSample | None = None
        failed_sample: ModelLivenessJoinSample | None = None

        try:
            async with pool.acquire() as conn:
                for row in rows:
                    checked += 1
                    correlation_id = row["correlation_id"]
                    input_ref = ModelEventRef(
                        topic=demand.locator,
                        partition=row["partition"],
                        offset=row["kafka_offset"],
                        event_id=row["ledger_entry_id"],
                    )
                    # The join key is known regardless of whether a matching
                    # row is found (it is the value we searched on), so
                    # projection_key_canonical is populated on both the
                    # success and failure samples -- ModelLivenessReceipt
                    # requires it non-None for HEALTHY *and* RED (design §5:
                    # only terminal_event_ref/projection_value_hash/
                    # projection_expected_value_hash are HEALTHY-only).
                    key_canonical = json.dumps(
                        {"correlation_id": str(correlation_id)}, sort_keys=True
                    )
                    match = await conn.fetchrow(join_sql, correlation_id)
                    if match is None:
                        failed += 1
                        if failed_sample is None:
                            failed_sample = ModelLivenessJoinSample(
                                correlation_id=correlation_id,
                                input_event_ref=input_ref,
                                projection_key_canonical=key_canonical,
                                expected_value_predicate_result=False,
                            )
                        continue
                    if healthy_sample is None:
                        observed_hash = _sha256_json(
                            {
                                "ledger_entry_id": match["ledger_entry_id"],
                                "partition": match["partition"],
                                "kafka_offset": match["kafka_offset"],
                            }
                        )
                        expected_hash = hashlib.sha256(
                            join.expected_value_predicate.encode()
                        ).hexdigest()
                        healthy_sample = ModelLivenessJoinSample(
                            correlation_id=correlation_id,
                            input_event_ref=input_ref,
                            terminal_event_ref=ModelEventRef(
                                topic=join.terminal_topic,
                                partition=match["partition"],
                                offset=match["kafka_offset"],
                                event_id=match["ledger_entry_id"],
                            ),
                            projection_key_canonical=key_canonical,
                            projection_value_hash=observed_hash,
                            projection_expected_value_hash=expected_hash,
                            expected_value_predicate_result=True,
                        )
        except Exception as exc:
            logger.warning(
                "liveness_demand_query_effect: correlation join query failed "
                "for surface_id=%s: %s",
                surface_id,
                exc,
            )
            return self._not_ready(surface_id, f"correlation join query failed: {exc}")

        logger.info(
            "liveness_demand_query_effect: surface_id=%s eligible=%d checked=%d "
            "failed=%d",
            surface_id,
            eligible_count,
            checked,
            failed,
        )
        return ModelLivenessDemandQueryResult(
            surface_id=surface_id,
            query_succeeded=True,
            eligible_count=eligible_count,
            checked_count=checked,
            failed_count=failed,
            demand_query_evidence=query_evidence,
            healthy_sample=healthy_sample,
            failed_sample=failed_sample,
        )


__all__ = ["HandlerLivenessDemandQueryEffect"]
