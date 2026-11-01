"""OMN-19972 step 29: the composed boot covers the run-trace writer.

omnibase_infra#5457 put ``omnimarket-projection-run-trace`` in the
``local-market`` bundle. The composed boot names the market projections it
requires in the render step, and the negative control stops each named
service to prove the health step fails without it. A writer missing from
either list could vanish from the bundle, or die, and the boot would pass.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
RUN_TRACE = "omnimarket-projection-run-trace"
RENDER_STEP = "Render the composed laptop bundle"
NEGATIVE_STEP = (
    "Stopped laptop service fails the health step and restoring it passes "
    "(negative control)"
)


@pytest.fixture
def job() -> dict[str, Any]:
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/runtime-boot-composed.yml").read_text()
    )
    assert isinstance(workflow, dict)
    found = workflow["jobs"]["composed-runtime-boot"]
    assert isinstance(found, dict)
    return found


def step_run(job: dict[str, Any], name: str) -> str:
    matches = [s for s in job["steps"] if s.get("name") == name]
    assert len(matches) == 1, name
    run = matches[0]["run"]
    assert isinstance(run, str)
    return run


@pytest.mark.unit
def test_render_requires_the_run_trace_writer(job: dict[str, Any]) -> None:
    render = step_run(job, RENDER_STEP)
    block = re.search(r"required = \{(.*?)\}", render, re.DOTALL)
    assert block, "render step has no required set"
    required = set(re.findall(r'"([^"]+)"', block.group(1)))
    assert RUN_TRACE in required


@pytest.mark.unit
def test_negative_control_stops_the_run_trace_writer(job: dict[str, Any]) -> None:
    negative = step_run(job, NEGATIVE_STEP)
    loop = re.search(r"for svc in ([^;]+); do", negative)
    assert loop, "negative control has no service loop"
    assert RUN_TRACE in loop.group(1).split()
