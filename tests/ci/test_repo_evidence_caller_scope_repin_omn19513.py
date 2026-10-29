# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-19513: pin the receipt-gate reusable that scopes dod-verify to new evidence."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

CALLER_PATH = (
    Path(__file__).resolve().parents[2]
    / ".github"
    / "workflows"
    / "call-repo-evidence-gate.yml"
)

# omnibase_core#2079 scopes evidence; #2077 gives dod-verify 60 minutes.
# Umbrella-contract PRs had dod-verify cancelled at the 40-minute limit because
# every carried item ran at the head and again at the merge base.
_SCOPE_REUSABLE_TAG = "dev-20261022114039-578228977721"
_PREVIOUS_REUSABLE_SHA = "4d16322dae9d62f96089b93421a768fb8dd8c8fe"

_REUSABLE = "OmniNode-ai/omnibase_core/.github/workflows/receipt-gate.yml"


def _job() -> dict[str, object]:
    caller = yaml.safe_load(CALLER_PATH.read_text(encoding="utf-8"))
    job: dict[str, object] = caller["jobs"]["repo-evidence"]
    return job


def test_caller_pins_the_reusable_that_scopes_dod_verify_to_new_evidence() -> None:
    assert _job()["uses"] == f"{_REUSABLE}@{_SCOPE_REUSABLE_TAG}"


def test_caller_no_longer_pins_the_reusable_with_the_40_minute_budget() -> None:
    uses = _job()["uses"]
    assert isinstance(uses, str)
    assert not uses.endswith(f"@{_PREVIOUS_REUSABLE_SHA}")


def test_the_scope_repin_keeps_the_job_id_and_inputs() -> None:
    job = _job()
    assert job["with"] == {
        "evidence-source": "caller",
        "verifier-version": "0.4.305",
        "compare-with-occ": "false",
    }
