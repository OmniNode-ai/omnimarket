# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-17427: repository prose lives in the public knowledge base.

The contribution, security and changelog guides and the unused README, table
and evidence text files are gone from this repository, and the root README is a
short signpost that links the public knowledge base.
"""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
KNOWLEDGE_BASE_URL = "https://github.com/OmniNode-ai/knowledge_base"

REMOVED_PROSE = (
    ".github/CONTRIBUTING.md",
    "CHANGELOG.md",
    "SECURITY.md",
    "benchmarks/delegation_false_pass_growth/README.txt",
    "benchmarks/delegation_ladder/results/rungs/2026-10-14-hard/README.txt",
    "benchmarks/delegation_ladder/results/rungs/2026-10-14-hard/table.txt",
    "benchmarks/delegation_ladder/results/rungs/README.txt",
    "docs/tracking/2026-05-03-ab-compare-suite-all-models.txt",
    "formal/ci_red_triage/README.txt",
    "formal/delegation_eval_run/README.txt",
    "formal/devhead_guard/README.txt",
    "formal/durable_evidence_delivery/README.txt",
    "tests/fixtures/delegation/omn18297/README.md",
    "tests/fixtures/delegation/omn18379/README.md",
    "tests/fixtures/delegation/omn19433/README.md",
    "tests/fixtures/lab_proof_receipts/README.md",
)


@pytest.mark.parametrize("relative_path", REMOVED_PROSE)
def test_removed_prose_file_is_absent(relative_path: str) -> None:
    assert not (REPO_ROOT / relative_path).exists()


def test_root_readme_is_a_short_signpost_to_the_knowledge_base() -> None:
    lines = (REPO_ROOT / "README.md").read_text(encoding="utf-8").splitlines()
    assert 3 <= len(lines) <= 5
    assert KNOWLEDGE_BASE_URL in "\n".join(lines)
