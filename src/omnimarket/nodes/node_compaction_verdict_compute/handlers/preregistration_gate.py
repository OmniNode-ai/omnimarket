# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The refusals that make a confirmatory run honour the CM.14 pre-registration. Pure.

A confirmatory run is refused unless:

* the data file carries the pre-registration block (the recorded calibration, the
  split, the tolerance, the progress-call classes, the build-selection rule and the
  per-safeguard margins) and every safeguard of section 3.8 has a margin in it;
* the data file's text hashes to the file the merged data PR fixed, and that PR was
  merged before the run started;
* every boundary read belongs to the held-out split (a calibration session, or one
  in neither set, is refused);
* the build it judges is the one the data file's rule selects from the builds'
  calibration-split scores, and that build has no confirmatory verdict yet.
"""

from __future__ import annotations

import hashlib

from omnimarket.nodes.node_compaction_verdict_compute.models import (
    CONFIRMATORY_SAFEGUARDS,
    CompactionVerdictRefusedError,
    EnumFloorDirection,
    ModelBuildCandidate,
    ModelCompactionThresholds,
    ModelCompactionVerdictRequest,
    ModelPreregistration,
)

__all__ = ["select_build", "verify_confirmatory"]


def _meets_floors(candidate: ModelBuildCandidate, prereg: ModelPreregistration) -> bool:
    for floor in prereg.build_selection.floors:
        score = candidate.calibration_scores.get(floor.criterion)
        if score is None:
            return False
        if floor.direction is EnumFloorDirection.HIGHER and score < floor.value:
            return False
        if floor.direction is EnumFloorDirection.LOWER and score > floor.value:
            return False
    return True


def select_build(
    prereg: ModelPreregistration, candidates: tuple[ModelBuildCandidate, ...]
) -> ModelBuildCandidate:
    """Among builds meeting every floor, the highest selection-metric score on the
    calibration split; ties go to the later commit, then to the larger candidate id."""
    metric = prereg.build_selection.selection_metric
    eligible = [
        c
        for c in candidates
        if _meets_floors(c, prereg) and metric in c.calibration_scores
    ]
    if not eligible:
        raise CompactionVerdictRefusedError(
            "confirmatory run refused: no build meets every floor of the build-selection rule"
        )
    return max(
        eligible,
        key=lambda c: (c.calibration_scores[metric], c.commit_time, c.candidate_id),
    )


def verify_confirmatory(
    request: ModelCompactionVerdictRequest, thresholds: ModelCompactionThresholds
) -> ModelBuildCandidate:
    """Refuse a confirmatory run that breaks the pre-registration; return the judged build."""
    prereg = thresholds.preregistration
    if prereg is None:
        raise CompactionVerdictRefusedError(
            "confirmatory run refused: the data file carries no preregistration block"
        )
    names = {s.name for s in thresholds.safeguards}
    absent = [n for n in CONFIRMATORY_SAFEGUARDS if n not in names]
    if absent:
        raise CompactionVerdictRefusedError(
            f"confirmatory run refused: safeguard {absent[0]!r} has no margin in the data file"
        )

    record, started = request.registration, request.run_started_at
    if record is None or started is None:
        raise CompactionVerdictRefusedError(
            "confirmatory run refused: no merged data PR record or run start time"
        )
    if record.merged_at.tzinfo is None or started.tzinfo is None:
        raise CompactionVerdictRefusedError(
            "confirmatory run refused: the data PR merge time and run start must carry a zone"
        )
    digest = hashlib.sha256(request.thresholds_text.encode("utf-8")).hexdigest()
    if digest != record.data_file_sha256:
        raise CompactionVerdictRefusedError(
            "confirmatory run refused: the data file's hash is not the one the merged "
            f"data PR {record.data_pr} fixed"
        )
    if record.merged_at >= started:
        raise CompactionVerdictRefusedError(
            f"confirmatory run refused: data PR {record.data_pr} was not merged before the run"
        )

    held_out = set(prereg.split.held_out_sessions)
    calibration = set(prereg.split.calibration_sessions)
    for session in sorted({c.session_id for c in request.boundaries}):
        if session in calibration:
            raise CompactionVerdictRefusedError(
                f"confirmatory run refused: session {session!r} is a calibration session"
            )
        if session not in held_out:
            raise CompactionVerdictRefusedError(
                f"confirmatory run refused: session {session!r} is not in the held-out split"
            )

    if request.build is None:
        raise CompactionVerdictRefusedError(
            "confirmatory run refused: the request names no build to judge"
        )
    selected = select_build(prereg, request.build_candidates)
    if request.build != selected.candidate_id:
        raise CompactionVerdictRefusedError(
            f"confirmatory run refused: build {request.build!r} is not the build the "
            f"selection rule picks ({selected.candidate_id!r})"
        )
    if selected.candidate_id in request.prior_confirmatory_builds:
        raise CompactionVerdictRefusedError(
            f"confirmatory run refused: build {selected.candidate_id!r} already has "
            "a confirmatory verdict"
        )
    return selected
