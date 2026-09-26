# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The hostile-reviewer verdict step reads the reviewer's quorum (OMN-18479).

The step's inline verdict parser is EXTRACTED from the workflow and
EXECUTED against fixture payloads. A test that greps the YAML for the word
``quorum`` would pass on a comment that merely mentions it; this one runs
the parser and reads what it prints.

Two properties are pinned:

* a ``degraded_quorum`` verdict -- fewer models succeeded than agreement
  requires, so there is no verdict at all -- never reads as ``passed``;
* the blocking count comes from the reviewer's agreement clusters, not
  from a severity sum across models, which is the rule that let a single
  model's finding block a merge.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

WORKFLOW = (
    Path(__file__).resolve().parents[2]
    / ".github"
    / "workflows"
    / "hostile-reviewer.yml"
)


def _extract_verdict_snippet() -> str:
    text = WORKFLOW.read_text(encoding="utf-8")
    start = text.index('VERDICT_DATA=$(REVIEW_JSON="$REVIEW_JSON" python3 - ')
    body = text[text.index("\n", start) + 1 : text.index("PYEOF\n", start)]
    return "\n".join(
        line[10:] if line.startswith(" " * 10) else line for line in body.splitlines()
    )


def _run(payload: dict[str, Any]) -> dict[str, str]:
    completed = subprocess.run(
        [sys.executable, "-c", _extract_verdict_snippet()],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
        env={"REVIEW_JSON": json.dumps(payload), "PATH": "/usr/bin:/bin"},
    )
    return dict(
        line.split("=", 1) for line in completed.stdout.splitlines() if "=" in line
    )


def test_degraded_quorum_never_reads_as_passed() -> None:
    out = _run(
        {
            "models_succeeded": ["qwen3-review"],
            "total_findings": 1,
            "quorum": {
                "verdict": "degraded_quorum",
                "blocking_count": 0,
                "warning_count": 1,
            },
        }
    )
    assert out["verdict"] == "degraded"
    assert out["quorum_verdict"] == "degraded_quorum"


def test_agreed_finding_blocks() -> None:
    """Positive control: a genuine two-model agreement still blocks."""
    out = _run(
        {
            "models_succeeded": ["qwen3-review", "gpt-oss-review"],
            "total_findings": 2,
            "quorum": {
                "verdict": "blocked",
                "blocking_count": 1,
                "warning_count": 0,
            },
        }
    )
    assert out["verdict"] == "blocked"
    assert out["blocking_count"] == "1"


def test_single_model_finding_does_not_block() -> None:
    out = _run(
        {
            "models_succeeded": ["qwen3-review", "gpt-oss-review"],
            "total_findings": 1,
            "quorum": {
                "verdict": "passed",
                "blocking_count": 0,
                "warning_count": 1,
            },
        }
    )
    assert out["verdict"] == "passed"
    assert out["blocking_count"] == "0"
    assert out["below_quorum_count"] == "1"


def test_absent_quorum_falls_back_to_the_model_count_rule() -> None:
    """A reviewer predating OMN-18479 must not read as degraded."""
    out = _run(
        {
            "models_succeeded": ["qwen3-review", "gpt-oss-review"],
            "total_findings": 0,
        }
    )
    assert out["verdict"] == "passed"
    assert out["quorum_verdict"] == "absent"
