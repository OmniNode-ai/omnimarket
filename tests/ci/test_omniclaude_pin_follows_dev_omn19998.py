# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19998: omniclaude consumes omnimarket by git pin, and no gate waits for a release.

Operator ruling 2026-10-16T07:34:18Z: "There's no reason we need to wait for a
release." omniclaude pins omnimarket to a dev commit in its uv.lock. Two
wirings here follow from that:

* a merge to dev dispatches ``omnimarket-dev-moved`` to omniclaude, whose
  sibling-lock-refresh.yml advances the pin (the event type is spelled in both
  repositories; a cross-repo test would skip wherever the other clone is
  absent, so each side pins its own spelling);
* the Wire Compatibility Gate grades the commit omniclaude pins, read from
  omniclaude dev's uv.lock, instead of the last release tag.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
DISPATCH_TYPE = "omnimarket-dev-moved"
GATE_SCRIPT = "scripts/ci/check_wire_compatibility.py"


def _load(name: str) -> dict[Any, Any]:
    loaded = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _gate_step_run() -> str:
    steps = _load("ci.yml")["jobs"]["wire-compatibility-gate"]["steps"]
    runs = [
        str(s.get("run", "")) for s in steps if GATE_SCRIPT in str(s.get("run", ""))
    ]
    assert len(runs) == 1, "the wire gate step is missing or duplicated"
    return runs[0]


def test_a_dev_merge_dispatches_the_pin_refresh_to_omniclaude() -> None:
    job = _load("runtime-rebuild-trigger.yml")["jobs"]["notify-omniclaude-pin"]
    condition = " ".join(str(job["if"]).split())
    assert "github.event.pull_request.merged == true" in condition
    assert "github.event.pull_request.base.ref == 'dev'" in condition
    # Public repository: hosted runners only (operator ruling 2026-10-16T07:07:09Z).
    assert job["runs-on"] == "ubuntu-latest"
    runs = "\n".join(str(s.get("run", "")) for s in job["steps"])
    assert "repos/OmniNode-ai/omniclaude/dispatches" in runs
    assert f"event_type={DISPATCH_TYPE}" in runs
    mint = next(s for s in job["steps"] if s.get("id") == "app-token")
    assert mint["with"]["repositories"] == "omniclaude"
    assert mint["with"]["permission-contents"] == "write"


def test_the_wire_gate_grades_omniclaudes_pinned_commit() -> None:
    run = _gate_step_run()
    assert "raw.githubusercontent.com/OmniNode-ai/omniclaude/dev/uv.lock" in run
    assert '--consumer-ref "$pin"' in run
    # An unreadable pin refuses; it never falls back to the last tag.
    assert "exit 1" in run


def _extraction_pipeline() -> str:
    line = next(
        ln.strip()
        for ln in _gate_step_run().splitlines()
        if ln.strip().startswith("pin=")
    )
    # pin="$( printf ... "$lock" | grep ... )" -> the pipeline inside $( ).
    return line[len('pin="$(') : -len(')"')]


@pytest.mark.parametrize(
    ("lock_text", "expected"),
    [
        (
            'source = { git = "https://github.com/OmniNode-ai/omnimarket.git?rev='
            + "d" * 40
            + "#"
            + "d" * 40
            + '" }\n',
            "d" * 40,
        ),
        ('source = { registry = "https://pypi.org/simple" }\n', ""),
    ],
)
def test_the_pin_extraction_reads_omniclaudes_lock_format(
    lock_text: str, expected: str
) -> None:
    """Dry run of the exact extraction the step runs, on omniclaude's lock shape.

    The negative row is the control: a registry source yields no pin, which
    the step then refuses.
    """
    done = subprocess.run(
        ["bash", "-c", f"set -o pipefail; {_extraction_pipeline()} || true"],
        env={"lock": lock_text, "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=True,
    )
    assert done.stdout.strip() == expected
