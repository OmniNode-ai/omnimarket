#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Single source of truth for the coverage-shard artifact metadata shape (OMN-14762 / F-19-B).

Before this module, the shard metadata sidecar (``coverage-meta-<split>.json``)
was built by INLINE python inside ``.github/workflows/ci.yml`` while the shape
it had to match was enforced only DOWNSTREAM by
``aggregate_coverage_artifacts.validate_artifacts``. The producer and the
validator therefore drifted silently: a shape mistake in the workflow's inline
emit was caught in CI (``ARTIFACT_MALFORMED`` / ``ARTIFACT_SCHEMA_MISMATCH``),
never locally — exactly the F-19 review-class-caught-late friction.

This module OWNS the schema version and the metadata dict construction. Both the
producer (``emit_coverage_shard_metadata.py``, called by the CI emit step) and
the validator (``aggregate_coverage_artifacts.py``, which imports
``SCHEMA_VERSION`` from here) now consume the same code, and a round-trip test
(``tests/ci/test_coverage_artifact_metadata_roundtrip.py``) proves the emitted
shape passes ``validate_artifacts``. It is intentionally stdlib-only (no
omnimarket import chain) so the lightweight CI emit step does not have to import
the heavy aggregator.
"""

from __future__ import annotations

from datetime import UTC, datetime

# The one place this string lives. ``aggregate_coverage_artifacts`` imports it
# from here so producer and validator can never disagree on the version tag.
SCHEMA_VERSION = "omnimarket.coverage-artifact/v1"

# Keys ``validate_artifacts`` treats as required. Kept here so the round-trip
# test and the producer share one definition of "complete".
REQUIRED_KEYS = ("schema_version", "head_sha", "split", "split_count", "test_scope")


def derive_test_scope(smart_flag: str | None, is_full: str | None) -> str:
    """Derive the consensus ``test_scope`` from the CI env flags.

    ``smart`` (``vars.ENABLE_SMART_TESTS == "true"``) selects a subset unless the
    change-detector already escalated to the full suite (``is_full``). Any other
    combination is ``"full"``. Mirrors the logic that previously lived inline in
    ci.yml so the producer's scope is now testable.
    """
    smart = smart_flag == "true"
    full = is_full == "true"
    return "smart" if (smart and not full) else "full"


def build_shard_metadata(
    *,
    head_sha: str,
    split: int,
    split_count: int,
    test_scope: str,
    run_id: str | None = None,
    run_attempt: str | None = None,
    runner_os: str | None = None,
    python_version: str = "3.12",
    now: datetime | None = None,
) -> dict[str, object]:
    """Build the coverage-shard metadata sidecar dict.

    The returned dict is exactly what the shard uploads as
    ``coverage-meta-<split>.json`` and what
    ``aggregate_coverage_artifacts.validate_artifacts`` consumes. Every
    ``validate_artifacts``-required key is populated with a correctly typed
    value; ``created_at`` is emitted so the aggregator's ``--max-age-seconds``
    staleness defence has a timestamp to check.
    """
    if test_scope not in {"full", "smart"}:
        raise ValueError(f"test_scope must be 'full' or 'smart', got {test_scope!r}")
    stamp = (now or datetime.now(UTC)).isoformat()
    return {
        "schema_version": SCHEMA_VERSION,
        "head_sha": head_sha,
        "split": int(split),
        "split_count": int(split_count),
        "test_scope": test_scope,
        "run_id": run_id,
        "run_attempt": run_attempt,
        "python_version": python_version,
        "runner_os": runner_os,
        "created_at": stamp,
    }
