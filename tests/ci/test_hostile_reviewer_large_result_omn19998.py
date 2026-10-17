# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The hostile-reviewer verdict step reads a large review result (OMN-19998).

cli_review prints the whole ModelMultiReviewResult, every finding's text
included. The verdict step used to hand that JSON to its parser as one
environment string; Linux refuses any single argument or environment string
over 128 KiB at exec, so a large review died with "Argument list too long"
(exit 126), wrote no verdict and failed the gate as ``unknown`` on a PR the
reviewer had finished reading.

The step's shell block is EXTRACTED from the workflow and EXECUTED under
bash, with the result as the review step leaves it, so the test fails on
the exec refusal itself rather than on a grep of the YAML.
"""

from __future__ import annotations

import json
import shutil
import subprocess
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

# Over the kernel's 128 KiB per-string limit (MAX_ARG_STRLEN) with room to
# spare: the failing runs carried review results of this order.
_LARGE_BYTES = 300_000


def _extract_verdict_block() -> str:
    """The ``VERDICT_DATA=$(...)`` block, heredoc and closing paren included."""
    text = WORKFLOW.read_text(encoding="utf-8")
    start = text.rindex("\n", 0, text.index("VERDICT_DATA=$(")) + 1
    heredoc_end = text.index("          PYEOF\n", start)
    end = text.index("          )\n", heredoc_end) + len("          )\n")
    return "\n".join(
        line[10:] if line.startswith(" " * 10) else line
        for line in text[start:end].splitlines()
    )


def _payload(size: int) -> dict[str, Any]:
    finding = {"title": "x" * 200, "description": "y" * 800}
    count = max(1, size // 1100)
    return {
        "models_succeeded": ["qwen3-review", "local-studio-planner"],
        "total_findings": count,
        "findings": [finding] * count,
        "quorum": {"verdict": "blocked", "blocking_count": 1, "warning_count": 3},
    }


def _run(tmp_path: Path, payload: dict[str, Any]) -> dict[str, str]:
    result = tmp_path / "hostile-reviewer-result.json"
    result.write_text(json.dumps(payload), encoding="utf-8")
    script = "\n".join(
        [
            "set -euo pipefail",
            # The review step's two forms of the result: the file cli_review's
            # stdout is written to, and the shell variable the step held before.
            f"REVIEW_JSON_FILE={json.dumps(str(result))}",
            'REVIEW_JSON="$(cat "$REVIEW_JSON_FILE")"',
            _extract_verdict_block(),
            'printf "%s\\n" "$VERDICT_DATA"',
        ]
    )
    bash = shutil.which("bash")
    assert bash is not None
    completed = subprocess.run(
        [bash, "-c", script],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
        cwd=tmp_path,
    )
    assert completed.returncode == 0, completed.stderr
    return dict(
        line.split("=", 1) for line in completed.stdout.splitlines() if "=" in line
    )


def test_large_review_result_yields_a_verdict(tmp_path: Path) -> None:
    payload = _payload(_LARGE_BYTES)
    assert len(json.dumps(payload)) > 128 * 1024
    out = _run(tmp_path, payload)
    assert out["verdict"] == "blocked"
    assert out["blocking_count"] == "1"
    assert out["below_quorum_count"] == "3"


def test_small_review_result_still_yields_a_verdict(tmp_path: Path) -> None:
    """Positive control: the ordinary size reads the same way."""
    payload = _payload(2_000)
    payload["quorum"] = {"verdict": "passed", "blocking_count": 0, "warning_count": 0}
    out = _run(tmp_path, payload)
    assert out["verdict"] == "passed"
    assert out["models_succeeded"] == "qwen3-review,local-studio-planner"


def test_review_result_is_written_to_a_file_not_captured_in_a_variable() -> None:
    """The review step keeps cli_review's stdout out of argv and the environment."""
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "REVIEW_JSON=$(uv run" not in text
    assert 'REVIEW_JSON="$REVIEW_JSON"' not in text
    assert '> "$REVIEW_JSON_FILE"' in text
