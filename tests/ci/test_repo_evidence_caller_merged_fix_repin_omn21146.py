# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-21146: pin the receipt-gate reusable whose merged-fix control skips contract files."""

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

# omnibase_core#1986: the squash commit whose Binds-Merged-Fix control filters
# contracts/OMN-<n>.yaml out of the product paths it reverts. Before it, a fix
# whose contract the base had since amended failed `git apply -R` and the
# control refused with "do not revert cleanly at the merge base".
# The pin moved to a descendant that keeps this: omnibase_core#2079 (OMN-19513).
_MERGED_FIX_REUSABLE_TAG = "dev-20261022114039-578228977721"

# omnibase_core#1932: the previous pin, whose control counts the contract as a
# product path.
_PREVIOUS_REUSABLE_SHA = "4e4f5e0404d364e296c5d37db6aa3ab9e07f8ffa"

_REUSABLE = "OmniNode-ai/omnibase_core/.github/workflows/receipt-gate.yml"


def _job() -> dict[str, object]:
    caller = yaml.safe_load(CALLER_PATH.read_text(encoding="utf-8"))
    job: dict[str, object] = caller["jobs"]["repo-evidence"]
    return job


def test_caller_pins_the_reusable_whose_merged_fix_control_skips_contract_files() -> (
    None
):
    assert _job()["uses"] == f"{_REUSABLE}@{_MERGED_FIX_REUSABLE_TAG}", (
        "pin the omnibase_core receipt-gate reusable at or past omnibase_core#1986"
    )


def test_caller_no_longer_pins_the_reusable_that_reverts_contract_files() -> None:
    uses = _job()["uses"]
    assert isinstance(uses, str)
    assert not uses.endswith(f"@{_PREVIOUS_REUSABLE_SHA}")


def test_the_repin_keeps_the_job_id_and_inputs() -> None:
    job = _job()
    assert job["with"] == {
        "evidence-source": "caller",
        "verifier-version": "0.4.305",
        "compare-with-occ": "false",
    }
