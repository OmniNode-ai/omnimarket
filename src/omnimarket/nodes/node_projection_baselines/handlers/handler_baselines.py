"""Baselines projection: Kafka -> 4 tables transactionally."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from omnimarket.projection.runner import (
    BaseProjectionRunner,
    MessageMeta,
    deterministic_correlation_id,
    safe_parse_date,
)

logger = logging.getLogger(__name__)

TOPIC = "onex.evt.omnibase-infra.baselines-computed.v1"

UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I
)
MAX_BATCH_ROWS = 4000
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
VALID_PROMOTION_ACTIONS = {"promote", "shadow", "demote", "retire", "hold"}
VALID_CONFIDENCE_LEVELS = {"low", "medium", "high"}


class BaselinesProjectionRunner(BaseProjectionRunner):
    """Projects baselines-computed events into 4 tables transactionally.

    Tables: baselines_snapshots, baselines_comparisons, baselines_trend, baselines_breakdown
    Uses DELETE+INSERT for child tables within a transaction.
    Matches omnidash projectBaselinesSnapshot() exactly.
    """

    def handle(self, input_data: dict[str, Any]) -> dict[str, Any]:
        """RuntimeLocal handler protocol shim.

        Delegates to project_event via asyncio.run().
        """
        topic = str(input_data.pop("_topic", TOPIC))
        meta = MessageMeta(
            partition=int(input_data.pop("_partition", 0)),
            offset=int(input_data.pop("_offset", 0)),
            fallback_id=str(input_data.pop("_fallback_id", "")),
        )
        ok = asyncio.run(self.project_event(topic, input_data, meta))
        return {"projected": ok}

    @property
    def topics(self) -> list[str]:
        return [TOPIC]

    async def project_event(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        raw_snapshot_id = data.get("snapshot_id")
        if raw_snapshot_id and UUID_RE.match(str(raw_snapshot_id)):
            snapshot_id = str(raw_snapshot_id)
        else:
            snapshot_id = deterministic_correlation_id(
                "baselines-computed", meta.partition, meta.offset
            )

        contract_version = _safe_int(data.get("contract_version"), 1)
        computed_at_utc = safe_parse_date(
            data.get("computed_at_utc")
            or data.get("computedAtUtc")
            or data.get("computed_at")
        )
        window_start_utc = (
            safe_parse_date(data.get("window_start_utc") or data.get("windowStartUtc"))
            if (data.get("window_start_utc") or data.get("windowStartUtc"))
            else None
        )
        window_end_utc = (
            safe_parse_date(data.get("window_end_utc") or data.get("windowEndUtc"))
            if (data.get("window_end_utc") or data.get("windowEndUtc"))
            else None
        )

        # Parse child arrays with batch caps
        _raw_comp = data.get("comparisons")
        raw_comparisons_all: list[Any] = (
            _raw_comp if isinstance(_raw_comp, list) else []
        )
        if len(raw_comparisons_all) > MAX_BATCH_ROWS:
            logger.warning(
                "baselines snapshot %s: %d comparison rows, capping at %d",
                snapshot_id,
                len(raw_comparisons_all),
                MAX_BATCH_ROWS,
            )
        raw_comparisons = raw_comparisons_all[:MAX_BATCH_ROWS]

        _raw_tr = data.get("trend")
        raw_trend_all: list[Any] = _raw_tr if isinstance(_raw_tr, list) else []
        if len(raw_trend_all) > MAX_BATCH_ROWS:
            logger.warning(
                "baselines snapshot %s: %d trend rows, capping at %d",
                snapshot_id,
                len(raw_trend_all),
                MAX_BATCH_ROWS,
            )
        raw_trend = raw_trend_all[:MAX_BATCH_ROWS]

        _raw_bd = data.get("breakdown")
        raw_breakdown_all: list[Any] = _raw_bd if isinstance(_raw_bd, list) else []
        if len(raw_breakdown_all) > MAX_BATCH_ROWS:
            logger.warning(
                "baselines snapshot %s: %d breakdown rows, capping at %d",
                snapshot_id,
                len(raw_breakdown_all),
                MAX_BATCH_ROWS,
            )
        raw_breakdown = raw_breakdown_all[:MAX_BATCH_ROWS]

        # Build trend rows with validation and dedup
        trend_by_date: dict[str, dict[str, Any]] = {}
        for t in raw_trend:
            if not isinstance(t, dict):
                continue
            date_val = str(t.get("date") or t.get("dateStr") or "")
            if not date_val or not DATE_RE.match(date_val):
                logger.warning("Skipping trend row with invalid date: %s", date_val)
                continue
            trend_by_date[date_val] = {
                "snapshot_id": snapshot_id,
                "date": date_val,
                "avg_cost_savings": str(
                    max(
                        0.0,
                        min(
                            99.0,
                            _safe_float(
                                t.get("avg_cost_savings") or t.get("avgCostSavings")
                            ),
                        ),
                    )
                ),
                "avg_outcome_improvement": str(
                    max(
                        0.0,
                        min(
                            99.0,
                            _safe_float(
                                t.get("avg_outcome_improvement")
                                or t.get("avgOutcomeImprovement")
                            ),
                        ),
                    )
                ),
                "comparisons_evaluated": _safe_int(
                    t.get("comparisons_evaluated") or t.get("comparisonsEvaluated")
                ),
            }

        # Build comparison rows
        comparison_rows = []
        for c in raw_comparisons:
            if not isinstance(c, dict):
                continue
            pattern_id = str(c.get("pattern_id") or c.get("patternId") or "").strip()
            if not pattern_id:
                logger.warning(
                    "Skipping comparison row with blank pattern_id for snapshot %s",
                    snapshot_id,
                )
                continue

            rec_raw = str(c.get("recommendation") or "")
            recommendation = rec_raw if rec_raw in VALID_PROMOTION_ACTIONS else "shadow"
            conf_raw = str(c.get("confidence") or "").lower()
            confidence = conf_raw if conf_raw in VALID_CONFIDENCE_LEVELS else "low"

            comparison_rows.append(
                {
                    "snapshot_id": snapshot_id,
                    "pattern_id": pattern_id,
                    "pattern_name": str(
                        c.get("pattern_name") or c.get("patternName") or ""
                    ),
                    "sample_size": _safe_int(
                        c.get("sample_size") or c.get("sampleSize")
                    ),
                    "window_start": str(
                        c.get("window_start") or c.get("windowStart") or ""
                    ),
                    "window_end": str(c.get("window_end") or c.get("windowEnd") or ""),
                    "token_delta": json.dumps(
                        c.get("token_delta") or c.get("tokenDelta") or {}
                    ),
                    "time_delta": json.dumps(
                        c.get("time_delta") or c.get("timeDelta") or {}
                    ),
                    "retry_delta": json.dumps(
                        c.get("retry_delta") or c.get("retryDelta") or {}
                    ),
                    "test_pass_rate_delta": json.dumps(
                        c.get("test_pass_rate_delta")
                        or c.get("testPassRateDelta")
                        or {}
                    ),
                    "review_iteration_delta": json.dumps(
                        c.get("review_iteration_delta")
                        or c.get("reviewIterationDelta")
                        or {}
                    ),
                    "recommendation": recommendation,
                    "confidence": confidence,
                    "rationale": str(c.get("rationale") or ""),
                }
            )

        # Build breakdown rows with dedup
        breakdown_by_action: dict[str, dict[str, Any]] = {}
        for b in raw_breakdown:
            if not isinstance(b, dict):
                continue
            action_raw = str(b.get("action") or "")
            action = action_raw if action_raw in VALID_PROMOTION_ACTIONS else "shadow"
            breakdown_by_action[action] = {
                "snapshot_id": snapshot_id,
                "action": action,
                "count": _safe_int(b.get("count")),
                "avg_confidence": str(
                    max(
                        0.0,
                        min(
                            1.0,
                            _safe_float(
                                b.get("avg_confidence") or b.get("avgConfidence")
                            ),
                        ),
                    )
                ),
            }

        # Execute all in a single transaction
        queries: list[tuple[str, tuple[Any, ...]]] = []

        # 1. Upsert snapshot header
        queries.append(
            (
                """
            INSERT INTO baselines_snapshots (
              snapshot_id, contract_version, computed_at_utc, window_start_utc, window_end_utc
            ) VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (snapshot_id) DO UPDATE SET
              contract_version = EXCLUDED.contract_version,
              computed_at_utc = EXCLUDED.computed_at_utc,
              window_start_utc = EXCLUDED.window_start_utc,
              window_end_utc = EXCLUDED.window_end_utc,
              projected_at = NOW()
            """,
                (
                    snapshot_id,
                    contract_version,
                    computed_at_utc,
                    window_start_utc,
                    window_end_utc,
                ),
            )
        )

        # 2. Delete + re-insert comparisons
        queries.append(
            (
                "DELETE FROM baselines_comparisons WHERE snapshot_id = $1",
                (snapshot_id,),
            )
        )
        for row in comparison_rows:
            queries.append(
                (
                    """
                INSERT INTO baselines_comparisons (
                  snapshot_id, pattern_id, pattern_name, sample_size,
                  window_start, window_end, token_delta, time_delta,
                  retry_delta, test_pass_rate_delta, review_iteration_delta,
                  recommendation, confidence, rationale
                ) VALUES (
                  $1, $2, $3, $4, $5, $6, $7::jsonb, $8::jsonb,
                  $9::jsonb, $10::jsonb, $11::jsonb, $12, $13, $14
                )
                """,
                    (
                        row["snapshot_id"],
                        row["pattern_id"],
                        row["pattern_name"],
                        row["sample_size"],
                        row["window_start"],
                        row["window_end"],
                        row["token_delta"],
                        row["time_delta"],
                        row["retry_delta"],
                        row["test_pass_rate_delta"],
                        row["review_iteration_delta"],
                        row["recommendation"],
                        row["confidence"],
                        row["rationale"],
                    ),
                )
            )

        # 3. Delete + re-insert trend
        queries.append(
            (
                "DELETE FROM baselines_trend WHERE snapshot_id = $1",
                (snapshot_id,),
            )
        )
        for row in trend_by_date.values():
            queries.append(
                (
                    """
                INSERT INTO baselines_trend (
                  snapshot_id, date, avg_cost_savings, avg_outcome_improvement, comparisons_evaluated
                ) VALUES ($1, $2, $3, $4, $5)
                """,
                    (
                        row["snapshot_id"],
                        row["date"],
                        row["avg_cost_savings"],
                        row["avg_outcome_improvement"],
                        row["comparisons_evaluated"],
                    ),
                )
            )

        # 4. Delete + re-insert breakdown
        queries.append(
            (
                "DELETE FROM baselines_breakdown WHERE snapshot_id = $1",
                (snapshot_id,),
            )
        )
        for row in breakdown_by_action.values():
            queries.append(
                (
                    """
                INSERT INTO baselines_breakdown (
                  snapshot_id, action, count, avg_confidence
                ) VALUES ($1, $2, $3, $4)
                """,
                    (
                        row["snapshot_id"],
                        row["action"],
                        row["count"],
                        row["avg_confidence"],
                    ),
                )
            )

        await self.db.execute_in_transaction(queries)

        logger.info(
            "Projected baselines snapshot %s (%d comparisons, %d trend, %d breakdown)",
            snapshot_id,
            len(comparison_rows),
            len(trend_by_date),
            len(breakdown_by_action),
        )
        return True


def _safe_int(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    try:
        return int(float(value))
    except (ValueError, TypeError):
        return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (ValueError, TypeError):
        return default


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    runner = BaselinesProjectionRunner()
    asyncio.run(runner.run())
