# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-12818 — url-authority gate CLI (ratchet enforcement entry point)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnimarket.nodes.node_aislop_sweep.handlers.url_authority_gate import main

_VIOLATION = 'LINEAR_API_URL = "https://api.linear.app/graphql"\n'
_CLEAN = 'x = resolve_integration("linear").base_url\n'


def _seed_repo(tmp_path: Path, content: str) -> tuple[Path, Path]:
    """Build a tiny repo with one source file and return (repo_root, baseline_path)."""
    src = tmp_path / "repo" / "src"
    src.mkdir(parents=True)
    (src / "handler.py").write_text(content, encoding="utf-8")
    baseline = tmp_path / "baseline.json"
    return tmp_path / "repo", baseline


@pytest.mark.unit
class TestGateCli:
    def test_seed_then_grandfathered_pass(self, tmp_path: Path) -> None:
        repo_root, baseline = _seed_repo(tmp_path, _VIOLATION)
        # Seed the baseline from the existing violation.
        rc = main(
            [
                "--seed",
                "--repo",
                "r",
                "--repo-root",
                str(repo_root),
                "--baseline",
                str(baseline),
            ]
        )
        assert rc == 0
        assert json.loads(baseline.read_text())["count"] == 1

        # Now a full-repo gate run passes — the violation is grandfathered.
        rc = main(
            [
                "--all",
                "--repo",
                "r",
                "--repo-root",
                str(repo_root),
                "--baseline",
                str(baseline),
            ]
        )
        assert rc == 0

    def test_new_violation_fails(self, tmp_path: Path) -> None:
        repo_root, baseline = _seed_repo(tmp_path, _CLEAN)
        main(
            [
                "--seed",
                "--repo",
                "r",
                "--repo-root",
                str(repo_root),
                "--baseline",
                str(baseline),
            ]
        )
        # Add a NEW violation file and gate it explicitly (staged path).
        new_file = repo_root / "src" / "new_handler.py"
        new_file.write_text(_VIOLATION, encoding="utf-8")
        rc = main(
            [
                str(new_file),
                "--repo",
                "r",
                "--repo-root",
                str(repo_root),
                "--baseline",
                str(baseline),
            ]
        )
        assert rc == 1, "a NEW violation absent from the baseline must fail the gate"

    def test_update_rejects_growth(self, tmp_path: Path) -> None:
        repo_root, baseline = _seed_repo(tmp_path, _CLEAN)
        main(
            [
                "--seed",
                "--repo",
                "r",
                "--repo-root",
                str(repo_root),
                "--baseline",
                str(baseline),
            ]
        )
        # Introduce a new violation, then try to regenerate the baseline — the
        # shrink-only guard must reject the growth (can't whitelist fresh debt).
        (repo_root / "src" / "handler.py").write_text(_VIOLATION, encoding="utf-8")
        rc = main(
            [
                "--update-baseline",
                "--repo",
                "r",
                "--repo-root",
                str(repo_root),
                "--baseline",
                str(baseline),
            ]
        )
        assert rc == 1, "regenerating a GROWN baseline must be rejected"

    def test_clean_repo_passes(self, tmp_path: Path) -> None:
        repo_root, baseline = _seed_repo(tmp_path, _CLEAN)
        rc = main(
            [
                "--all",
                "--repo",
                "r",
                "--repo-root",
                str(repo_root),
                "--baseline",
                str(baseline),
            ]
        )
        assert rc == 0

    def test_cross_repo_entries_preserved(self, tmp_path: Path) -> None:
        # A baseline carrying another repo's entry must keep it after a re-seed.
        repo_root, baseline = _seed_repo(tmp_path, _VIOLATION)
        baseline.write_text(
            json.dumps(
                {
                    "schema_version": "1.0.0",
                    "count": 1,
                    "violations": [
                        {
                            "repo": "other",
                            "path": "src/x.py",
                            "rule": "public-https-literal",
                            "fingerprint": "deadbeef",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        main(
            [
                "--seed",
                "--repo",
                "r",
                "--repo-root",
                str(repo_root),
                "--baseline",
                str(baseline),
            ]
        )
        doc = json.loads(baseline.read_text())
        repos = {e["repo"] for e in doc["violations"]}
        assert "other" in repos, "other-repo baseline entries must be preserved"
        assert "r" in repos
