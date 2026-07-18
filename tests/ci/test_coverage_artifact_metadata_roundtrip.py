# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Producer<->validator round-trip for the coverage-shard metadata (OMN-14762 / F-19-B).

F-19-B: the ``coverage-meta-<split>.json`` sidecar was built by INLINE python in
``ci.yml`` while its required shape was enforced only downstream by
``aggregate_coverage_artifacts.validate_artifacts``. A shape mistake was caught
in CI, never locally. The fix routes the emit through the shared producer
``coverage_artifact_metadata.build_shard_metadata`` and imports ``SCHEMA_VERSION``
into the validator, so this test can prove the emitted shape validates — a drift
now fails a local test.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts" / "ci"))

import aggregate_coverage_artifacts as agg  # noqa: E402
from coverage_artifact_metadata import (  # noqa: E402
    SCHEMA_VERSION,
    build_shard_metadata,
    derive_test_scope,
)

HEAD = "0" * 40


def _write_shard(artifacts_dir: Path, meta: dict[str, object]) -> None:
    split = int(meta["split"])  # type: ignore[call-overload]
    (artifacts_dir / f"coverage-meta-{split}.json").write_text("{}", encoding="utf-8")
    # validate_artifacts requires the coverage data file to physically exist.
    (artifacts_dir / f".coverage.{split}").write_text("data", encoding="utf-8")


class TestSchemaVersionSingleSource:
    def test_validator_imports_producer_schema_version(self) -> None:
        # The validator's SCHEMA_VERSION IS the producer's — one source of truth.
        assert agg.SCHEMA_VERSION == SCHEMA_VERSION


class TestProducedMetadataValidates:
    """GREEN: build_shard_metadata output passes validate_artifacts unchanged."""

    def test_single_shard_roundtrip(self, tmp_path: Path) -> None:
        meta = build_shard_metadata(
            head_sha=HEAD,
            split=1,
            split_count=1,
            test_scope="full",
            run_id="123",
            run_attempt="1",
            runner_os="Linux",
        )
        _write_shard(tmp_path, meta)

        scope = agg.validate_artifacts(
            [meta], tmp_path, expected_head=HEAD, split_count=1
        )
        assert scope == "full"

    def test_multi_shard_roundtrip(self, tmp_path: Path) -> None:
        metas = [
            build_shard_metadata(
                head_sha=HEAD, split=s, split_count=2, test_scope="smart"
            )
            for s in (1, 2)
        ]
        for meta in metas:
            _write_shard(tmp_path, meta)

        scope = agg.validate_artifacts(
            metas, tmp_path, expected_head=HEAD, split_count=2
        )
        assert scope == "smart"

    def test_produced_metadata_passes_max_age_check(self, tmp_path: Path) -> None:
        # created_at is emitted, so the staleness defence has a timestamp to read.
        meta = build_shard_metadata(
            head_sha=HEAD, split=1, split_count=1, test_scope="full"
        )
        _write_shard(tmp_path, meta)
        scope = agg.validate_artifacts(
            [meta], tmp_path, expected_head=HEAD, split_count=1, max_age_seconds=3600
        )
        assert scope == "full"


class TestShapeDriftIsRejected:
    """RED: a shape drift the old inline emit could introduce must be rejected."""

    def test_missing_required_key_rejected(self, tmp_path: Path) -> None:
        meta = build_shard_metadata(
            head_sha=HEAD, split=1, split_count=1, test_scope="full"
        )
        del meta["test_scope"]  # simulate producer dropping a required key
        _write_shard(tmp_path, meta)
        with pytest.raises(agg.ArtifactValidationError) as exc:
            agg.validate_artifacts([meta], tmp_path, expected_head=HEAD, split_count=1)
        assert exc.value.reason == agg.REASON_MALFORMED

    def test_wrong_schema_version_rejected(self, tmp_path: Path) -> None:
        meta = build_shard_metadata(
            head_sha=HEAD, split=1, split_count=1, test_scope="full"
        )
        meta["schema_version"] = "omnimarket.coverage-artifact/v0"
        _write_shard(tmp_path, meta)
        with pytest.raises(agg.ArtifactValidationError) as exc:
            agg.validate_artifacts([meta], tmp_path, expected_head=HEAD, split_count=1)
        assert exc.value.reason == agg.REASON_SCHEMA_MISMATCH


class TestDeriveTestScope:
    @pytest.mark.parametrize(
        ("smart", "is_full", "expected"),
        [
            ("true", "false", "smart"),
            ("true", "true", "full"),  # escalation to full wins
            ("false", "false", "full"),
            (None, None, "full"),
            ("false", "true", "full"),
        ],
    )
    def test_scope_truth_table(
        self, smart: str | None, is_full: str | None, expected: str
    ) -> None:
        assert derive_test_scope(smart, is_full) == expected

    def test_invalid_scope_rejected_by_builder(self) -> None:
        with pytest.raises(ValueError, match="test_scope"):
            build_shard_metadata(
                head_sha=HEAD, split=1, split_count=1, test_scope="bogus"
            )
