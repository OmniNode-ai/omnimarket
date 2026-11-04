# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerTestDurationsRefresh - keep the test-durations record ahead of its cap (OMN-22389).

``config/test_file_durations.json`` plans the full-suite shards. Every new test
file is "unrecorded" until someone records it, and
``tests/unit/test_ci_shard_plan_record_freshness_omn17427.py`` fails every open PR
once 50 files are unrecorded. The cap was hit three times on 2026-11-03.

This handler is the pure half of the dev-side refresh. ``status`` counts the
unrecorded files and says whether the refresh is due (at ``refresh_at`` files,
well under the cap). ``supplement`` returns the record with the unrecorded files
added from one measured run; every existing entry is left exactly as it was, and
a file with no measured seconds (every test deselected) is recorded at a token
weight so it stops counting as stale. The cap is never read or changed here.
"""

from __future__ import annotations

from collections.abc import Iterable

from omnimarket.nodes.node_test_durations_refresh_effect.models.model_test_durations_refresh import (
    EnumTestDurationsOperation,
    ModelTestDurationsProvenanceEntry,
    ModelTestDurationsRefreshRequest,
    ModelTestDurationsRefreshResult,
)

UNMEASURED_FILE_SECONDS = 0.01
_PROVENANCE_KEY_SHA_LENGTH = 12


def file_durations(test_durations: dict[str, float]) -> dict[str, float]:
    """Sum per-test seconds (``path::test`` ids) into per-file seconds."""
    totals: dict[str, float] = {}
    for test_id, seconds in test_durations.items():
        test_file = test_id.split("::", 1)[0]
        totals[test_file] = totals.get(test_file, 0.0) + seconds
    return totals


def unrecorded_files(
    test_files: Iterable[str], record: dict[str, float]
) -> tuple[str, ...]:
    """The test files the record does not hold, sorted."""
    return tuple(sorted(set(test_files) - record.keys()))


class HandlerTestDurationsRefresh:
    """Count the unrecorded test files, or add them to the record."""

    def handle(
        self, request: ModelTestDurationsRefreshRequest
    ) -> ModelTestDurationsRefreshResult:
        unrecorded = unrecorded_files(request.test_files, request.record)
        status = ModelTestDurationsRefreshResult(
            operation=request.operation,
            test_file_count=len(set(request.test_files)),
            unrecorded=unrecorded,
            refresh_at=request.refresh_at,
            due=len(unrecorded) >= request.refresh_at,
        )
        if request.operation is EnumTestDurationsOperation.STATUS or not unrecorded:
            return status

        measured = file_durations(request.measured)
        updated = dict(request.record)
        measured_files: list[str] = []
        unmeasured_files: list[str] = []
        for name in unrecorded:
            if measured.get(name, 0.0) > 0:
                updated[name] = round(measured[name], 2)
                measured_files.append(name)
            else:
                updated[name] = UNMEASURED_FILE_SECONDS
                unmeasured_files.append(name)
        entry = ModelTestDurationsProvenanceEntry(
            ticket=request.ticket,
            recorded_on=request.recorded_on,
            files_added=len(unrecorded),
            files_without_measurement=len(unmeasured_files),
            basis=(
                "files the record did not hold, each the sum of its measured pytest "
                "setup, call and teardown durations from one serial run of those files "
                "with --store-durations -m 'not kafka' --timeout=300 --no-cov; a file "
                f"with no measured test is recorded at {UNMEASURED_FILE_SECONDS} s; "
                "existing entries left as they were"
            ),
            runner=request.runner,
            base_sha=request.base_sha,
        )
        return status.model_copy(
            update={
                "measured_files": tuple(measured_files),
                "unmeasured_files": tuple(unmeasured_files),
                "updated_record": dict(sorted(updated.items())),
                "provenance_key": (
                    f"supplement_dev_refresh_{request.base_sha[:_PROVENANCE_KEY_SHA_LENGTH]}"
                ),
                "provenance_entry": entry,
            }
        )


__all__ = [
    "UNMEASURED_FILE_SECONDS",
    "HandlerTestDurationsRefresh",
    "file_durations",
    "unrecorded_files",
]
