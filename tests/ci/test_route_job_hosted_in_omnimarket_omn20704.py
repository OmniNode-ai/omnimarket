# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20704: the ci.yml route job calls the reusable workflow hosted here."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

pytestmark = pytest.mark.unit

CI_WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"
ROUTE_USES = re.compile(
    r"^OmniNode-ai/omnimarket/\.github/workflows/route-runner-reusable\.yml@[0-9a-f]{40}$"
)


def _route_job() -> dict[str, Any]:
    workflow = cast(dict[str, Any], yaml.safe_load(CI_WORKFLOW.read_text()))
    return cast(dict[str, Any], workflow["jobs"]["route"])


def test_the_route_job_calls_the_omnimarket_hosted_reusable_at_a_full_sha() -> None:
    assert ROUTE_USES.match(str(_route_job()["uses"]))


def test_the_route_job_keeps_its_check_name() -> None:
    assert _route_job()["name"] == "Runner Route (OMN-18031)"
