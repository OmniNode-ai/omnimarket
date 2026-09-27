# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-17492: the hostile reviewer's roster is two different lab models.

Until this change every step of this workflow named ``qwen3-review`` and
``qwen3-review-b``. Both keys resolve to one endpoint, ``.201:8000``, which
serves one model (Qwen3.8-27B), so the two-model quorum (OMN-18479) was that
model agreeing with itself. On the OMN-17492 eval (2026-09-25), the same
model on two hosts agreed on a critical finding in 2 of 8 runs and both were
false; paired with gpt-oss-120b it blocked nothing.

The roster is now ``qwen3-review`` (Qwen3.8-27B, .201) and ``gpt-oss-review``
(gpt-oss-120b on the .200 Mac Studio, registered in omniintelligence#945).
Both are lab models: private diffs never go to a third-party cloud reviewer
(operator, 2026-09-25; OPERATOR-CONSENT on the rolling ledger, row 4842).
.200 is always on, so there is no single-model pass: a lost reviewer leaves
one model, no quorum, and the "Enforce hostile-review verdict" step fails the
job (OMN-15110).

The roster must be identical in the three places that name it: the
reachability preflight, the retry-budget invariant and the review itself.
A step that probes or budgets a different set from the one the review runs
checks the wrong thing.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

WORKFLOW = (
    Path(__file__).resolve().parents[2]
    / ".github"
    / "workflows"
    / "hostile-reviewer.yml"
)
ROSTER = ["qwen3-review", "gpt-oss-review"]
NOT_VOTERS = ("qwen3-review-b", "deepseek-r1", "glm-review")


def _steps() -> list[dict[str, Any]]:
    parsed = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = parsed["jobs"]["hostile-review"]["steps"]
    assert isinstance(steps, list)
    return steps


def _step_named(prefix: str) -> dict[str, Any]:
    matches = [s for s in _steps() if str(s.get("name", "")).startswith(prefix)]
    assert len(matches) == 1, f"expected one step named {prefix!r}"
    return matches[0]


def _models(step: dict[str, Any]) -> list[str]:
    return re.findall(r"--model\s+([A-Za-z0-9_.-]+)", str(step["run"]))


def test_review_runs_the_two_lab_models() -> None:
    assert _models(_step_named("Run adversarial review")) == ROSTER


def test_retry_budget_covers_the_roster_the_review_runs() -> None:
    assert _models(_step_named("Validate retry-budget invariant")) == ROSTER


def test_preflight_probes_the_roster_the_review_runs() -> None:
    step = _step_named("Preflight")
    assert str(step["env"]["REVIEW_MODEL_KEYS"]).split() == ROSTER


@pytest.mark.parametrize("key", NOT_VOTERS)
def test_no_alias_and_no_cloud_model_is_a_voter(key: str) -> None:
    for prefix in ("Run adversarial review", "Validate retry-budget invariant"):
        assert key not in _models(_step_named(prefix))
    assert key not in str(_step_named("Preflight")["env"]["REVIEW_MODEL_KEYS"]).split()


def test_no_cloud_reviewer_credential_is_wired() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "LLM_GLM_API_KEY" not in text
    assert "LLM_CLOUD_ENDPOINT_HOST_ALLOWLIST" not in text
