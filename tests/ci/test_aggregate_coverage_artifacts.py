# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for scripts/ci/aggregate_coverage_artifacts.py (OMN-14680 / WS2).

FAIL-CLOSED PROOF: the shadow aggregator must go RED (reason-coded) against
every defective artifact class the ticket enumerates — wrong-head, missing
shard, missing data file, malformed metadata, schema skew, stale — and only
GREEN over a complete, head-bound, freshly measured shard set. A green over a
partial/mis-bound census would under-count coverage silently, which is exactly
the false-clean class this gate exists to prevent.

The combine path is proven end-to-end WITHOUT a second full pass: two real
per-shard data files are produced by ``coverage run`` over disjoint drivers
(exactly how pytest-cov measures a shard), combined ONCE into a single
``coverage.json``, and swept — demonstrating the WS2 invariant that coverage is
collected once (in the shards) and merged arithmetically, never regenerated.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from coverage import CoverageData

REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts" / "ci"))

import aggregate_coverage_artifacts as agg  # noqa: E402

HEAD = "a" * 40
OTHER_HEAD = "b" * 40


def _meta(
    split: int, split_count: int, *, head: str = HEAD, **overrides: object
) -> dict:
    meta = {
        "schema_version": agg.SCHEMA_VERSION,
        "head_sha": head,
        "split": split,
        "split_count": split_count,
        "test_scope": "full",
        "created_at": datetime.now(UTC).isoformat(),
    }
    meta.update(overrides)
    return meta


def _write_shard(artifacts_dir: Path, split: int, split_count: int, **kw) -> None:
    """Write a metadata sidecar + a (touch) data file for one shard."""
    meta = _meta(split, split_count, **kw)
    (artifacts_dir / f"coverage-meta-{split}.json").write_text(json.dumps(meta))
    (artifacts_dir / f".coverage.{split}").write_text("")  # presence only


def _write_full_set(artifacts_dir: Path, split_count: int, **kw) -> None:
    for s in range(1, split_count + 1):
        _write_shard(artifacts_dir, s, split_count, **kw)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------
def test_validation_accepts_complete_head_bound_set(tmp_path: Path) -> None:
    _write_full_set(tmp_path, 3)
    metas = agg.load_metadata(tmp_path)
    scope = agg.validate_artifacts(metas, tmp_path, expected_head=HEAD, split_count=3)
    assert scope == "full"


# ---------------------------------------------------------------------------
# Fail-closed: the enumerated defective-artifact classes
# ---------------------------------------------------------------------------
def test_wrong_head_fails_closed(tmp_path: Path) -> None:
    _write_full_set(tmp_path, 2)
    # Rebind split 2 to a different head — stale / cross-head.
    (tmp_path / "coverage-meta-2.json").write_text(
        json.dumps(_meta(2, 2, head=OTHER_HEAD))
    )
    metas = agg.load_metadata(tmp_path)
    with pytest.raises(agg.ArtifactValidationError) as exc:
        agg.validate_artifacts(metas, tmp_path, expected_head=HEAD, split_count=2)
    assert exc.value.reason == agg.REASON_WRONG_HEAD


def test_missing_shard_fails_closed(tmp_path: Path) -> None:
    _write_shard(tmp_path, 1, 3)  # only 1 of 3
    metas = agg.load_metadata(tmp_path)
    with pytest.raises(agg.ArtifactValidationError) as exc:
        agg.validate_artifacts(metas, tmp_path, expected_head=HEAD, split_count=3)
    assert exc.value.reason == agg.REASON_MISSING


def test_no_artifacts_fails_closed(tmp_path: Path) -> None:
    metas = agg.load_metadata(tmp_path)  # empty dir
    with pytest.raises(agg.ArtifactValidationError) as exc:
        agg.validate_artifacts(metas, tmp_path, expected_head=HEAD, split_count=1)
    assert exc.value.reason == agg.REASON_MISSING


def test_missing_data_file_fails_closed(tmp_path: Path) -> None:
    _write_full_set(tmp_path, 2)
    (tmp_path / ".coverage.2").unlink()  # sidecar present, data lost
    metas = agg.load_metadata(tmp_path)
    with pytest.raises(agg.ArtifactValidationError) as exc:
        agg.validate_artifacts(metas, tmp_path, expected_head=HEAD, split_count=2)
    assert exc.value.reason == agg.REASON_MISSING


def test_malformed_metadata_json_fails_closed(tmp_path: Path) -> None:
    (tmp_path / "coverage-meta-1.json").write_text("{not valid json")
    (tmp_path / ".coverage.1").write_text("")
    with pytest.raises(agg.ArtifactValidationError) as exc:
        agg.load_metadata(tmp_path)
    assert exc.value.reason == agg.REASON_MALFORMED


def test_missing_required_key_fails_closed(tmp_path: Path) -> None:
    meta = _meta(1, 1)
    del meta["head_sha"]
    (tmp_path / "coverage-meta-1.json").write_text(json.dumps(meta))
    (tmp_path / ".coverage.1").write_text("")
    metas = agg.load_metadata(tmp_path)
    with pytest.raises(agg.ArtifactValidationError) as exc:
        agg.validate_artifacts(metas, tmp_path, expected_head=HEAD, split_count=1)
    assert exc.value.reason == agg.REASON_MALFORMED


def test_schema_mismatch_fails_closed(tmp_path: Path) -> None:
    _write_shard(tmp_path, 1, 1, schema_version="omnimarket.coverage-artifact/v0")
    metas = agg.load_metadata(tmp_path)
    with pytest.raises(agg.ArtifactValidationError) as exc:
        agg.validate_artifacts(metas, tmp_path, expected_head=HEAD, split_count=1)
    assert exc.value.reason == agg.REASON_SCHEMA_MISMATCH


def test_stale_artifact_fails_closed(tmp_path: Path) -> None:
    old = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    _write_shard(tmp_path, 1, 1, created_at=old)
    metas = agg.load_metadata(tmp_path)
    with pytest.raises(agg.ArtifactValidationError) as exc:
        agg.validate_artifacts(
            metas, tmp_path, expected_head=HEAD, split_count=1, max_age_seconds=3600
        )
    assert exc.value.reason == agg.REASON_STALE


def test_inconsistent_scope_fails_closed(tmp_path: Path) -> None:
    _write_shard(tmp_path, 1, 2, test_scope="full")
    _write_shard(tmp_path, 2, 2, test_scope="smart")
    metas = agg.load_metadata(tmp_path)
    with pytest.raises(agg.ArtifactValidationError) as exc:
        agg.validate_artifacts(metas, tmp_path, expected_head=HEAD, split_count=2)
    assert exc.value.reason == agg.REASON_MALFORMED


# ---------------------------------------------------------------------------
# Parity (one-sided): shadow must not LOSE coverage vs authoritative
# ---------------------------------------------------------------------------
def _write_coverage_json(path: Path, percent: float, covered: int, stmts: int) -> None:
    path.write_text(
        json.dumps(
            {
                "totals": {
                    "percent_covered": percent,
                    "covered_lines": covered,
                    "num_statements": stmts,
                }
            }
        )
    )


def test_parity_equal_passes(tmp_path: Path) -> None:
    _write_coverage_json(tmp_path / "shadow.json", 82.0, 820, 1000)
    _write_coverage_json(tmp_path / "auth.json", 82.0, 820, 1000)
    ok, report = agg.parity_compare(
        tmp_path / "shadow.json", tmp_path / "auth.json", tolerance=0.5
    )
    assert ok
    assert report["delta_percent"] == pytest.approx(0.0)


def test_parity_shadow_higher_passes(tmp_path: Path) -> None:
    # Shards ran integration tests the authoritative gate skips → superset.
    _write_coverage_json(tmp_path / "shadow.json", 85.0, 850, 1000)
    _write_coverage_json(tmp_path / "auth.json", 82.0, 820, 1000)
    ok, report = agg.parity_compare(
        tmp_path / "shadow.json", tmp_path / "auth.json", tolerance=0.5
    )
    assert ok
    assert report["delta_percent"] == pytest.approx(3.0)


def test_parity_shadow_lower_beyond_tolerance_fails(tmp_path: Path) -> None:
    _write_coverage_json(tmp_path / "shadow.json", 80.0, 800, 1000)
    _write_coverage_json(tmp_path / "auth.json", 82.0, 820, 1000)
    ok, _ = agg.parity_compare(
        tmp_path / "shadow.json", tmp_path / "auth.json", tolerance=0.5
    )
    assert not ok


def test_parity_within_tolerance_passes(tmp_path: Path) -> None:
    _write_coverage_json(tmp_path / "shadow.json", 81.7, 817, 1000)
    _write_coverage_json(tmp_path / "auth.json", 82.0, 820, 1000)
    ok, _ = agg.parity_compare(
        tmp_path / "shadow.json", tmp_path / "auth.json", tolerance=0.5
    )
    assert ok


# ---------------------------------------------------------------------------
# Combine ONCE end-to-end (no second pytest --cov pass)
# ---------------------------------------------------------------------------
def test_combine_merges_shard_data_once(tmp_path: Path) -> None:
    """Produce two REAL per-shard coverage data files (via ``coverage run`` over
    disjoint drivers — exactly how pytest-cov measures a shard), combine them
    ONCE into a single ``coverage.json``, and prove the union strictly exceeds
    either shard alone. This is the WS2 invariant: coverage is collected once in
    the shards and merged arithmetically, never regenerated by a second pass."""
    target = tmp_path / "repo"
    pkg = target / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "mod.py").write_text(
        "def a():\n    return 1\n\n\ndef b():\n    return 2\n\n\ndef c():\n    return 3\n"
    )
    # Disjoint drivers: shard 1 exercises a(); shard 2 exercises b().
    (target / "drive1.py").write_text("from pkg import mod\nmod.a()\n")
    (target / "drive2.py").write_text("from pkg import mod\nmod.b()\n")

    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()

    def _measure(driver: str, split: int) -> None:
        subprocess.run(
            [
                sys.executable,
                "-m",
                "coverage",
                "run",
                f"--data-file={artifacts / f'.coverage.{split}'}",
                "--source=pkg",
                driver,
            ],
            cwd=str(target),
            check=True,
        )
        (artifacts / f"coverage-meta-{split}.json").write_text(
            json.dumps(_meta(split, 2))
        )

    _measure("drive1.py", 1)
    _measure("drive2.py", 2)

    # Shard 1 alone: the baseline the union must beat.
    shard1_only = target / "shard1.json"
    ok, _ = agg.combine_coverage(artifacts, 1, target, shard1_only)
    assert ok
    shard1_covered = json.loads(shard1_only.read_text())["totals"]["covered_lines"]

    out = target / "coverage.json"
    ok, msg = agg.combine_coverage(artifacts, 2, target, out)
    assert ok, msg
    assert out.is_file()

    data = json.loads(out.read_text())
    # Union of shard 1 (a) + shard 2 (b) strictly exceeds shard 1 alone.
    assert data["totals"]["covered_lines"] > shard1_covered

    # And the canonical sweep handler can consume the combined artifact.
    result = agg.NodeCoverageSweep().handle(
        agg.CoverageSweepRequest(target_dirs=[str(target)], target_pct=50.0)
    )
    assert result.status in {"clean", "gaps_found"}
    assert result.total_modules >= 1


def test_combine_ignores_missing_third_party_source_records(tmp_path: Path) -> None:
    """Combined shard data may include C-extension/provider traces whose source
    paths are not present in the checkout. The shadow aggregate should still
    emit JSON instead of failing on those non-repo records."""
    target = tmp_path / "repo"
    target.mkdir()
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()

    data_file = artifacts / ".coverage.1"
    data = CoverageData(basename=str(data_file))
    data.add_lines({str(target / "src/dependency_injector/providers.pyx"): {1, 2}})
    data.write()
    (artifacts / "coverage-meta-1.json").write_text(json.dumps(_meta(1, 1)))

    out = target / "coverage.json"
    ok, msg = agg.combine_coverage(artifacts, 1, target, out)

    assert ok, msg
    assert out.is_file()
