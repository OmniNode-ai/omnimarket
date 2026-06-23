# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Projection replay/idempotence check handler (OMN-12884).

Deterministic compute surface: no I/O.  Given a list of projection events
(each carrying correlation_id + topic + table), classifies each
(correlation_id, table) pair according to the Phase 7 replay taxonomy:

  replay-proven    — the same correlation arrived more than once and the
                     dedupe key (correlation_id) ensured a single DB row.
  runtime-observed — seen exactly once; no replay attempt recorded.
  blocked          — insufficient evidence to classify (empty correlation_id).
  superseded       — a later event for the same (correlation_id, table) key
                     arrived at a strictly higher (partition, offset), which
                     the ON CONFLICT / UPSERT path would handle; the earlier
                     occurrence is superseded.

Dashboard-rendered classification is not computed here — that requires a live
projection API call and belongs in an effect node.
"""

from __future__ import annotations

from collections import defaultdict

from omnimarket.nodes.node_projection_replay_check_compute.models.model_replay_check import (
    EnumReplayStatus,
    ModelCorrelationReplayResult,
    ModelProjectionEvent,
    ModelReplayCheckRequest,
    ModelReplayCheckResult,
)


class HandlerProjectionReplayCheck:
    """Stateless compute handler: classify projection events for replay safety.

    Call :meth:`check` with a :class:`ModelReplayCheckRequest`; returns a
    :class:`ModelReplayCheckResult` with per-correlation classifications.
    """

    def check(self, request: ModelReplayCheckRequest) -> ModelReplayCheckResult:
        """Classify each (correlation_id, table) pair in *request*.

        Algorithm:
        1. Group events by (correlation_id, table).
        2. For each group:
           - 1 occurrence  → runtime-observed
           - 2+ occurrences with same partition/offset → replay-proven
             (exact same bus message delivered twice; dedupe key held).
           - 2+ occurrences with *increasing* (partition, offset) →
             superseded (a later write updated the upsert row; the earlier
             occurrence is superseded by the most recent one).
           - When correlation_id is empty/blank → blocked.
        3. Aggregates are summed; status is "findings" if any non-runtime-
           observed classification is present, otherwise "clean".
        """
        # group by (correlation_id, table)
        groups: dict[tuple[str, str], list[ModelProjectionEvent]] = defaultdict(list)
        for evt in request.events:
            groups[(evt.correlation_id, evt.table)].append(evt)

        results: list[ModelCorrelationReplayResult] = []

        for (correlation_id, table), occurrences in groups.items():
            result = _classify_group(correlation_id, table, occurrences)
            results.append(result)

        counts = _tally(results)
        has_non_observed = any(
            r.status
            not in {EnumReplayStatus.RUNTIME_OBSERVED, EnumReplayStatus.REPLAY_PROVEN}
            for r in results
        )

        return ModelReplayCheckResult(
            status="findings" if has_non_observed else "clean",
            total_correlations=len(results),
            replay_proven=counts[EnumReplayStatus.REPLAY_PROVEN],
            runtime_observed=counts[EnumReplayStatus.RUNTIME_OBSERVED],
            blocked=counts[EnumReplayStatus.BLOCKED],
            superseded=counts[EnumReplayStatus.SUPERSEDED],
            findings=tuple(
                r
                for r in results
                if r.status
                not in {
                    EnumReplayStatus.RUNTIME_OBSERVED,
                    EnumReplayStatus.REPLAY_PROVEN,
                }
            ),
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _classify_group(
    correlation_id: str,
    table: str,
    occurrences: list[ModelProjectionEvent],
) -> ModelCorrelationReplayResult:
    count = len(occurrences)

    if not correlation_id.strip():
        return ModelCorrelationReplayResult(
            correlation_id=correlation_id,
            table=table,
            status=EnumReplayStatus.BLOCKED,
            occurrence_count=count,
            dedupe_held=False,
            detail="correlation_id is empty; cannot classify",
        )

    if count == 1:
        return ModelCorrelationReplayResult(
            correlation_id=correlation_id,
            table=table,
            status=EnumReplayStatus.RUNTIME_OBSERVED,
            occurrence_count=count,
            dedupe_held=True,
            detail="single occurrence; no replay attempted",
        )

    # Multiple occurrences — determine whether they are exact replays or
    # superseding writes.
    unique_offsets: set[tuple[int, int]] = {
        (e.partition, e.offset) for e in occurrences
    }

    if len(unique_offsets) == 1:
        # All arrivals share the exact same (partition, offset): the same
        # Kafka message was re-delivered.  The ON CONFLICT / UPSERT path
        # ensures exactly one row in the DB → dedupe held.
        return ModelCorrelationReplayResult(
            correlation_id=correlation_id,
            table=table,
            status=EnumReplayStatus.REPLAY_PROVEN,
            occurrence_count=count,
            dedupe_held=True,
            detail=(
                f"{count} identical deliveries of "
                f"partition={occurrences[0].partition}/offset={occurrences[0].offset}; "
                "dedupe key held"
            ),
        )

    # Different (partition, offset) pairs: a later write supersedes the
    # earlier one via UPSERT.  The earlier occurrence is superseded.
    sorted_by_pos = sorted(occurrences, key=lambda e: (e.partition, e.offset))
    earliest = sorted_by_pos[0]
    latest = sorted_by_pos[-1]
    return ModelCorrelationReplayResult(
        correlation_id=correlation_id,
        table=table,
        status=EnumReplayStatus.SUPERSEDED,
        occurrence_count=count,
        dedupe_held=True,
        detail=(
            f"earliest at partition={earliest.partition}/offset={earliest.offset} "
            f"superseded by partition={latest.partition}/offset={latest.offset}; "
            "UPSERT conflict resolution applies"
        ),
    )


def _tally(
    results: list[ModelCorrelationReplayResult],
) -> dict[EnumReplayStatus, int]:
    counts: dict[EnumReplayStatus, int] = dict.fromkeys(EnumReplayStatus, 0)
    for r in results:
        counts[r.status] += 1
    return counts
