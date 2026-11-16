# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Claude Code delegation is one hook, proven end to end (OMN-23413 AC41, AC43).

Runs one_hook_delegation_proof.sh: real claude and the onex under test, in an
isolated HOME and CLAUDE_CONFIG_DIR under `env -i`, with a free local model.
Failure modes the script checks, each a line in the artifact:
  `on` registers anything but the prompt hook and its history helper, or a
  second `on` changes the settings; an ordinary prompt reaches Claude instead of
  delegation, or its answer names no model and run; a prompt needing the machine
  is answered by delegation; the run has no receipt, no delegation_events row
  with measured tokens, or is missing from the dashboard; the operator's state
  is written; an unreachable model blocks instead of passing to Claude; `off`
  leaves any owned row, history helper included; the operator's real Claude
  settings change.

It needs a signed-in isolated profile and a model, so it runs only when asked:
ONEX_E2E_REQUIRE_COMPLETE=1 and AC41_ROOT (see the script's header).
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.release_proof]

_SCRIPT = Path(__file__).with_name("one_hook_delegation_proof.sh")


def test_one_hook_delegation_end_to_end() -> None:
    if os.environ.get("ONEX_E2E_REQUIRE_COMPLETE") != "1" or not os.environ.get(
        "AC41_ROOT"
    ):
        pytest.skip("Needs ONEX_E2E_REQUIRE_COMPLETE=1 and AC41_ROOT")
    proc = subprocess.run(
        ["bash", str(_SCRIPT)],
        capture_output=True,
        text=True,
        timeout=1500,
        check=False,
    )
    lines = [line for line in proc.stdout.splitlines() if line.startswith("{")]
    assert lines, proc.stdout + proc.stderr
    artifact = Path(json.loads(lines[-1])["artifact"])
    result = json.loads(artifact.read_text())
    failed = [name for name, ok in result["checks"].items() if not ok]
    assert not failed, (failed, str(artifact))
    assert result["passed"], str(artifact)
    assert proc.returncode == 0, proc.stderr
