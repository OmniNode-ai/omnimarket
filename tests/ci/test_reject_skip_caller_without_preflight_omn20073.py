# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20073 - the skip-token scan no longer carries a change-control preflight.

omniclaude#2540 (squash ``4358450cc`` on omniclaude ``dev``) removed the nested
``occ-preflight`` job from ``reject-deploy-gate-skip.yml`` and the ``needs:``
edge that held the token scan behind it; its own test,
``tests/ci/test_reject_skip_workflow_occ_independence.py``, proves the pinned
file runs the scan with no preflight. This repository adopts that by its own
pin bump, and every place here that named the nested preflight's context
(``call-reject-skip-token / occ-preflight / eligibility``) stops naming it,
because nothing produces it any more. The token scan itself stays enforced
through CI Summary. S6 part 2 also deletes the standalone preflight caller
and removes its context from CI Summary and the required-checks manifest,
so PR admission requires repo-owned evidence without an OCC companion.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from scripts.ci import ci_summary_gate as gate

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
CALLER = REPO_ROOT / ".github" / "workflows" / "call-reject-skip.yml"
REQUIRED_CHECKS = REPO_ROOT / ".github" / "required-checks.yaml"
REUSABLE = "OmniNode-ai/omniclaude/.github/workflows/reject-deploy-gate-skip.yml"
# The squash commit of omniclaude#2540 on omniclaude dev.
PREFLIGHT_FREE_SHA = "4358450ccbba0cee11e390208dd0b8b1728e94ab"
NESTED_PREFLIGHT = "call-reject-skip-token / occ-preflight / eligibility"
SCAN = "call-reject-skip-token / scan / reject-skip-gate-token"
STANDALONE_PREFLIGHT = "occ-preflight / eligibility"


def _caller_refs() -> list[str]:
    pattern = re.compile(rf"{re.escape(REUSABLE)}@(?P<ref>[^\s\"'#]+)")
    return [
        match["ref"]
        for line in CALLER.read_text().splitlines()
        if not line.lstrip().startswith("#")
        for match in pattern.finditer(line)
    ]


def test_caller_pins_the_reusable_without_the_change_control_preflight() -> None:
    assert _caller_refs() == [PREFLIGHT_FREE_SHA]


def test_ci_summary_no_longer_expects_the_nested_preflight() -> None:
    for event in ("pull_request", "merge_group", None):
        expected = gate.expected_external_contexts(event)
        assert NESTED_PREFLIGHT not in expected, event
        assert SCAN in expected, event
        assert STANDALONE_PREFLIGHT not in expected, event


def test_required_checks_manifest_declares_no_nested_preflight_row() -> None:
    manifest = yaml.safe_load(REQUIRED_CHECKS.read_text())
    names = {row["name"] for row in manifest["gates"] if isinstance(row, dict)}
    assert NESTED_PREFLIGHT not in names
    assert SCAN in names
    assert STANDALONE_PREFLIGHT not in names
