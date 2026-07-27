# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Durable append-only OCC observation record + dedup projection (OMN-14851).

Scaffolds the storage-agnostic half of the N=10 real-doneness counter
(``docs/plans/2026-07-20-occ-real-doneness-working-path-and-liveness-plan.md``
WS3 step 2, "spine item 6"). This module owns exactly two ideas, deliberately
kept separate:

  * :class:`ModelOccObservationRecord` — the APPEND-ONLY raw log row. Keyed by
    the full 6-tuple ``(product_repo, product_pr_number, head_sha,
    policy_version, workflow_run_id, run_attempt)``. One immutable row per
    actual ``call-occ-attestation-observe.yml`` execution attempt, including
    fail-soft/not-clean ones — the raw log is a history, never overwritten.

  * :func:`project_qualifying_observations` — the DEDUPLICATED PROJECTION.
    Collapses the raw log down to exactly one deterministic representative
    :class:`~omnimarket.events.occ_autoauthor.ModelOccAutoauthorObservation`
    per distinct EXACT SOURCE TUPLE ``(product_repo, product_pr_number,
    head_sha, policy_version)`` — dropping ``workflow_run_id``/``run_attempt``
    so reruns of the same head_sha count once, never N times (WS3 step 7:
    "do not count repeated attempts, dry runs, legacy products"). The
    representative is the MOST RECENT attempt for that source tuple
    (deterministic tie-break on ``(workflow_run_id, run_attempt)``); it is
    NOT filtered by ``is_clean`` — a non-clean representative still resets
    the downstream window's streak exactly as ``aggregate_autoauthor_window``
    already does, unchanged (design intent: dedupe attempts, do not reinterpret
    cleanliness).

WHERE the append-only raw log durably lives (git-committed files, an external
database, a self-hosted-runner-backed store, ...) is an OPEN ARCHITECTURE
DECISION tracked on OMN-14851 and is explicitly OUT OF SCOPE here. This module
is pure data + pure functions — zero I/O, storage-agnostic — so whichever
surface is approved only needs to produce/consume
:class:`ModelOccObservationRecord` rows.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.events.occ_autoauthor import ModelOccAutoauthorObservation


class EnumOccVerificationPath(StrEnum):
    """How the observed product PR's proof was gated (OMN-14954, lane A7).

    The representative-N window requires a composition floor — >=3 merged-path
    plus >=1 runtime/deploy-gated observations inside the trailing clean streak
    — so each record must say which verification path produced it.

    ``UNSPECIFIED`` exists ONLY so rows persisted before this field still parse;
    it counts toward NO composition threshold (fail-closed — an unlabeled row
    can never help satisfy the representativeness criterion).
    """

    MERGED_PATH = "merged_path"
    RUNTIME_DEPLOY_GATED = "runtime_deploy_gated"
    UNSPECIFIED = "unspecified"


#: The full append-only raw-log identity: one row per actual workflow attempt.
OccObservationRawKey = tuple[str, int, str, str, int, int]

#: The exact source tuple the N=10 counter must treat as one distinct unit —
#: reruns (different workflow_run_id/run_attempt) of the SAME source tuple
#: collapse to a single qualifying observation.
OccObservationSourceTuple = tuple[str, int, str, str]


class ModelOccObservationRecord(BaseModel):
    """One immutable append-only row in the OCC observation trail (OMN-14851).

    Wraps the existing :class:`ModelOccAutoauthorObservation` payload with the
    identity fields needed to key the durable, append-only raw log. The
    payload's own ``product_repo``/``product_pr_number`` are retained on the
    nested observation (unchanged contract); this envelope adds the fields the
    payload does not carry: exact commit, policy version in effect, and the
    GitHub Actions execution identity (workflow_run_id, run_attempt) that
    proves this is a real attempt and not a synthesized/duplicate row.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    product_repo: str = Field(..., description="Product repo slug (owner/repo).")
    product_pr_number: int = Field(..., description="Product PR number observed.")
    head_sha: str = Field(
        ..., description="Exact product PR head commit SHA this attempt observed."
    )
    policy_version: str = Field(
        ..., description="Proof-policy version in effect for this observation attempt."
    )
    workflow_run_id: int = Field(
        ...,
        description="GitHub Actions workflow_run id for this attempt (github.run_id).",
    )
    run_attempt: int = Field(
        ..., description="GitHub Actions run attempt number (github.run_attempt)."
    )
    recorded_at: str = Field(
        ..., description="ISO-8601 timestamp the record was appended (injected)."
    )
    verification_path: EnumOccVerificationPath = Field(
        default=EnumOccVerificationPath.UNSPECIFIED,
        description=(
            "Which verification path gated the observed product PR "
            "(merged_path | runtime_deploy_gated). Defaults to 'unspecified' so "
            "pre-OMN-14954 rows still parse; 'unspecified' satisfies NO "
            "composition threshold (fail-closed)."
        ),
    )
    observation: ModelOccAutoauthorObservation = Field(
        ..., description="The observation payload produced by this attempt."
    )


def occ_observation_raw_key(record: ModelOccObservationRecord) -> OccObservationRawKey:
    """Pure: the full append-only identity of one raw-log row.

    Two records sharing this key are the SAME attempt (defensive dedup on
    double-ingestion); they are never the result of two genuinely distinct
    executions, since GitHub mints a fresh ``run_attempt`` on every rerun.
    """
    return (
        record.product_repo,
        record.product_pr_number,
        record.head_sha,
        record.policy_version,
        record.workflow_run_id,
        record.run_attempt,
    )


def occ_observation_source_tuple(
    record: ModelOccObservationRecord,
) -> OccObservationSourceTuple:
    """Pure: the exact source tuple the N=10 counter must treat as one unit.

    Drops ``workflow_run_id``/``run_attempt`` — every rerun of the identical
    ``(repo, pr, head_sha, policy_version)`` is the SAME source tuple, so it
    can contribute at most one qualifying observation to the window.
    """
    return (
        record.product_repo,
        record.product_pr_number,
        record.head_sha,
        record.policy_version,
    )


def project_qualifying_records(
    records: Iterable[ModelOccObservationRecord],
) -> tuple[ModelOccObservationRecord, ...]:
    """Pure: dedupe the raw append-only log to one RECORD per source tuple.

    Identical dedup semantics to :func:`project_qualifying_observations`, but
    the representatives keep their full envelope — tuple identity and
    ``verification_path`` — so the composition-aware window (OMN-14954) can
    count distinct tuples and the merged-path / runtime-deploy-gated floor.
    Output order matches the observation projection: sorted by
    ``(observation.observed_at, product_repo, product_pr_number)``.
    """
    by_raw_key: dict[OccObservationRawKey, ModelOccObservationRecord] = {}
    for record in records:
        by_raw_key[occ_observation_raw_key(record)] = record

    by_source_tuple: dict[OccObservationSourceTuple, ModelOccObservationRecord] = {}
    for record in by_raw_key.values():
        source_tuple = occ_observation_source_tuple(record)
        current = by_source_tuple.get(source_tuple)
        if current is None or (record.workflow_run_id, record.run_attempt) > (
            current.workflow_run_id,
            current.run_attempt,
        ):
            by_source_tuple[source_tuple] = record

    representatives = list(by_source_tuple.values())
    representatives.sort(
        key=lambda r: (
            r.observation.observed_at,
            r.product_repo,
            r.product_pr_number,
        )
    )
    return tuple(representatives)


def project_qualifying_observations(
    records: Iterable[ModelOccObservationRecord],
) -> tuple[ModelOccAutoauthorObservation, ...]:
    """Pure: dedupe the raw append-only log to one observation per source tuple.

    Deterministic regardless of input order or duplicate rows:

      1. Defensive dedup on the full raw key (``occ_observation_raw_key``) —
         an identical attempt ingested twice collapses to one row.
      2. Group survivors by the exact source tuple
         (``occ_observation_source_tuple``).
      3. Within each group, the representative is the row with the highest
         ``(workflow_run_id, run_attempt)`` — the MOST RECENT attempt at that
         exact source tuple. Earlier attempts (including fail-soft ones) are
         NOT lost — they remain in the raw log — but they do not shadow a
         later clean attempt, and they are never double-counted.
      4. The output is NOT filtered by ``is_clean``: a non-clean representative
         is passed through unchanged so the existing window aggregator
         (``aggregate_autoauthor_window``) still resets the streak on it,
         exactly as it does today for any non-clean observation.
      5. Output order is deterministic: sorted by
         ``(observed_at, product_repo, product_pr_number)`` on the underlying
         observation, matching the window aggregator's own sort key, so the
         projection composes with ``ModelOccAutoauthorWindowRequest.observations``
         with no behavior change downstream.

    Implemented as the stripped form of :func:`project_qualifying_records`
    (OMN-14954) — one dedup, two views.
    """
    return tuple(record.observation for record in project_qualifying_records(records))


__all__ = [
    "EnumOccVerificationPath",
    "ModelOccObservationRecord",
    "OccObservationRawKey",
    "OccObservationSourceTuple",
    "occ_observation_raw_key",
    "occ_observation_source_tuple",
    "project_qualifying_observations",
    "project_qualifying_records",
]
