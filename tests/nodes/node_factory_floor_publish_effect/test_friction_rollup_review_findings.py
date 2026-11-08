# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Review findings 5251:2 and 5251:3 on the friction rollup step (OMN-22478).

5251:2 the rollup step ran its source handler with no bound, so one slow read or one backtracking class pattern held
the whole publication; the step's own ``timeout_seconds`` must bound it and the step must report ``failed``.
5251:3 the Mac-load class rule matched any numeric load, so ``Mac load average was 2`` named a timeout cause whose
owner text says the load was 35 or more; the rule needs both a timeout and a load of 35 or more.
"""

from __future__ import annotations

import re
import threading
import time
from pathlib import Path
from typing import Any

import pytest
import yaml

import omnimarket.nodes.node_factory_floor_publish_effect.handlers.handler_factory_floor_publish as publish
from tests.nodes.node_factory_floor_publish_effect.test_friction_rollup_step import (
    OVERLAY,
    environ,
    friction_step,
    run_step,
)

pytestmark = pytest.mark.unit

RULE = "step-timed-out-on-loaded-mac"


def mac_load_pattern() -> re.Pattern[str]:
    rules = yaml.safe_load(OVERLAY.read_text(encoding="utf-8"))["class_rules"]
    return re.compile(next(r["pattern"] for r in rules if r["name"] == RULE))


@pytest.mark.parametrize(
    "text",
    [
        "deployment refused authentication; Mac load average was 2",
        "pr-create ran past 130 s with no output while the Mac load average was 3.5",
        "the build timed out and the Mac load average was 12",
        "the Mac load average was 40 while the lane read its inbox",
    ],
)
def test_error_incidental_or_low_load_does_not_name_the_timeout_cause(
    text: str,
) -> None:
    assert mac_load_pattern().search(text) is None


@pytest.mark.parametrize(
    "text",
    [
        "pr-create ran past 130 s with no output while the Mac load average was 35",
        "the push timed out at 600 s; Mac load near 79",
        "Mac load runs 120 and the step was cut",
        "pr-create ran past 130 s while the Mac load average was 35.2",
    ],
)
def test_golden_a_timeout_with_a_load_of_35_or_more_names_the_cause(text: str) -> None:
    assert mac_load_pattern().search(text) is not None


def test_error_a_rollup_past_its_timeout_fails_the_step_and_keeps_the_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = threading.Event()

    class SlowSource:
        def handle(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            release.wait(6)
            return {"classes": []}

    monkeypatch.setattr(publish, "HandlerFrictionRollupSource", SlowSource)
    step = friction_step()
    assert step.output_relpath is not None
    home_report = tmp_path / "home" / step.output_relpath
    home_report.parent.mkdir(parents=True)
    home_report.write_text('{"classes": ["previous"]}', encoding="utf-8")
    started = time.monotonic()
    try:
        status, detail, path = run_step(
            tmp_path,
            environ(tmp_path),
            step=step.model_copy(update={"timeout_seconds": 1}),
        )
    finally:
        release.set()
    elapsed = time.monotonic() - started
    assert status == "failed"
    assert "TimeoutError" in detail
    assert elapsed < 4, f"the step waited {elapsed:.1f} s for a 1 s budget"
    assert path.read_text(encoding="utf-8") == '{"classes": ["previous"]}'
