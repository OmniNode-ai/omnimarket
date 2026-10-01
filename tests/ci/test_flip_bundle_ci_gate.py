# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-20182: flip discovery requires a successfully fetched, verified base."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

_FETCH = "git fetch --no-tags --prune origin +refs/heads/dev:refs/remotes/origin/dev"
_VERIFY = "git rev-parse --verify origin/dev"
_GATE = "uv run python ../omnibase_core/scripts/ci/verify_flip_bundle.py"


def _assert_fail_closed_base_fetch(run: str) -> None:
    lines = [line.strip() for line in run.splitlines() if line.strip()]
    fetches = [line for line in lines if line.startswith("git fetch ")]
    assert fetches, "base fetch missing"
    assert all(not line.endswith("|| true") for line in fetches), (
        "fetch failure swallowed"
    )
    assert _FETCH in lines, "expected base fetch missing"
    assert _VERIFY in lines, "base verification missing or failure swallowed"
    gate_index = next(i for i, line in enumerate(lines) if line.startswith(_GATE))
    assert lines.index(_FETCH) < lines.index(_VERIFY) < gate_index, (
        "base checked too late"
    )


@pytest.mark.unit
def test_live_flip_bundle_step_fetches_and_verifies_before_gate() -> None:
    root = Path(__file__).resolve().parents[2]
    workflow = yaml.safe_load((root / ".github/workflows/ci.yml").read_text())
    step = next(
        step
        for step in workflow["jobs"]["lint"]["steps"]
        if step.get("name")
        == "Canonical-shape flip-bundle seam gate (OMN-15344 fan-out)"
    )
    assert not step.get("continue-on-error", False)
    _assert_fail_closed_base_fetch(step["run"])


@pytest.mark.unit
def test_assertion_accepts_successful_fetch_and_verification() -> None:
    _assert_fail_closed_base_fetch(f"{_FETCH}\n{_VERIFY}\n{_GATE}\n")


@pytest.mark.unit
@pytest.mark.parametrize(
    "run",
    [
        f"{_FETCH} || true\n{_VERIFY}\n{_GATE}\n",
        f"{_FETCH}\n{_GATE}\n",
        f"{_FETCH}\n{_GATE}\n{_VERIFY}\n",
        f"{_FETCH}\n{_VERIFY} || true\n{_GATE}\n",
    ],
    ids=["swallowed-fetch", "missing-verify", "late-verify", "swallowed-verify"],
)
def test_assertion_rejects_fail_open_synthetic_step(run: str) -> None:
    with pytest.raises(AssertionError):
        _assert_fail_closed_base_fetch(run)
