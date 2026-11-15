# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21262: ``onex setup`` says what happens when none of the chosen models answers.

AC1  the notice is printed before the first model question in an interactive run,
     and before any step in an unattended run; its text is one constant.

The fixtures (fresh HOME, stub key server, subprocess recorder) are the ones the
OMN-23207 model-step tests use.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from omnimarket.nodes.node_onex_setup_effect import cli as onex_setup_cli
from tests.unit.nodes.node_onex_setup_effect.test_onex_setup_model_step import (
    MODEL,
    Recorder,
    _setup,
    _unattended,
)

pytestmark = pytest.mark.unit

LADDER_NOTICE = getattr(onex_setup_cli, "LADDER_NOTICE", None)


def test_notice_precedes_the_first_model_question_interactive(
    home: Path, tmp_path: Path, server: str, recorder: Recorder
) -> None:
    assert isinstance(LADDER_NOTICE, str), "LADDER_NOTICE missing from setup's cli"
    assert LADDER_NOTICE, "LADDER_NOTICE is empty"
    result = _setup(
        ["--interactive", "--workspace", str(tmp_path / "ws")],
        # OMN-23413 asks before checking models with one-token requests: "y".
        stdin=f"local\n{server}\ny\n{MODEL}\n",
    )
    assert result.exit_code == 0, result.output
    assert LADDER_NOTICE in result.output, "notice not printed"
    assert result.output.index(LADDER_NOTICE) < result.output.index("Provider"), (
        "notice must come before the first model question"
    )


def test_notice_printed_before_any_step_unattended(
    home: Path, tmp_path: Path, server: str, recorder: Recorder
) -> None:
    assert isinstance(LADDER_NOTICE, str), "LADDER_NOTICE missing from setup's cli"
    assert LADDER_NOTICE, "LADDER_NOTICE is empty"
    args = _unattended(
        tmp_path / "ws", "--provider", "local", "--url", server, "--model", MODEL
    )
    result = _setup(args, stdin="")
    assert result.exit_code == 0, result.output
    assert LADDER_NOTICE in result.output, "notice not printed unattended"
    notice_at = result.output.index(LADDER_NOTICE)
    # Through the local runtime (OMN-24455) each step reads "[done|unchanged] <step>:".
    first_step = re.search(r"^\[(?:done|unchanged)\] ", result.output, re.MULTILINE)
    assert first_step is not None, result.output
    assert notice_at < first_step.start(), "notice must come before steps"
    assert notice_at < result.output.index("Receipt: "), (
        "notice must come before the receipt"
    )


def test_notice_text_says_the_task_goes_back_to_claude_code(
    home: Path, tmp_path: Path, server: str, recorder: Recorder
) -> None:
    assert isinstance(LADDER_NOTICE, str), "LADDER_NOTICE missing from setup's cli"
    assert LADDER_NOTICE, "LADDER_NOTICE is empty"
    assert "goes back to Claude Code" in LADDER_NOTICE
    assert "no savings" in LADDER_NOTICE


def test_notice_leaves_json_output_one_document(
    home: Path, tmp_path: Path, server: str, recorder: Recorder
) -> None:
    """OMN-23413 merge: under --json the notice goes to stderr, never into the JSON."""
    import json

    args = _unattended(
        tmp_path / "ws", "--provider", "local", "--url", server, "--model", MODEL
    )
    result = _setup([*args, "--json"], stdin="")
    assert result.exit_code == 0, result.output
    assert LADDER_NOTICE not in result.stdout
    assert LADDER_NOTICE in result.stderr
    assert isinstance(json.loads(result.stdout), dict)
