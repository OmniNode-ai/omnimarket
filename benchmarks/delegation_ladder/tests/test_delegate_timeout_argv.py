# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ladder sends ``--timeout`` to the delegate only when the caller asks for one.

The delegate refuses a request above its task class ceiling before any rung is
dispatched (``timeout_exceeds_task_class_ceiling``: review allows less than the
harness-wide default the ladder used to send). That refusal leaves no receipt
and no model call, so every review-class R3b task came back ``refused`` (OMN-18339).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from benchmarks.delegation_ladder import run_ladder as runner
from benchmarks.delegation_ladder.models import ModelTaskBundle

pytestmark = pytest.mark.unit

EXPLICIT_TIMEOUT_S = 7


def _review_bundle() -> ModelTaskBundle:
    return ModelTaskBundle(
        task_id="R3b-unit",
        rung="R3b",
        title="Select",
        task_type="review",
        provenance="unit control",
        prompt="Name the workflow.",
        scorer="exact_match",
        scorer_config={"accepted": ["workflow"]},
    )


def _capture_argv(
    monkeypatch: pytest.MonkeyPatch, timeout_s: int | None, tmp_path: Path
) -> list[str]:
    monkeypatch.setenv("OMNI_HOME", str(tmp_path))
    seen: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.append(list(argv))
        return subprocess.CompletedProcess(argv, 1, "", "refused")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    runner.invoke_local(_review_bundle(), tmp_path / "onex", timeout_s)
    assert len(seen) == 1
    return seen[0]


def test_local_path_omits_timeout_when_none_asked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    argv = _capture_argv(monkeypatch, None, tmp_path)
    assert "--timeout" not in argv
    assert "--task-type" in argv


def test_local_path_forwards_an_explicit_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    argv = _capture_argv(monkeypatch, EXPLICIT_TIMEOUT_S, tmp_path)
    assert argv[argv.index("--timeout") + 1] == str(EXPLICIT_TIMEOUT_S)


def test_client_wait_covers_the_default_and_an_explicit_timeout() -> None:
    assert runner.client_wait_s(None) == runner.CLIENT_WAIT_S + runner.CLIENT_SLACK_S
    assert runner.client_wait_s(EXPLICIT_TIMEOUT_S) == (
        EXPLICIT_TIMEOUT_S + runner.CLIENT_SLACK_S
    )
    assert runner.delegate_timeout_argv(None) == []


def test_local_path_runs_in_process_when_asked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OMNI_HOME", str(tmp_path))
    seen: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.append(list(argv))
        return subprocess.CompletedProcess(argv, 1, "", "refused")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    runner.invoke_local(_review_bundle(), tmp_path / "onex", None, in_process=True)
    argv = seen[0]
    flags = list(runner.IN_PROCESS_FLAGS)
    assert argv[argv.index(flags[0]) : argv.index(flags[0]) + len(flags)] == flags
    runner.invoke_local(_review_bundle(), tmp_path / "onex", None)
    assert "--locus" not in seen[1]


def _terminal_receipt(response: str, *, gate_passed: bool) -> str:
    import json

    payload = {
        "status": "completed",
        "response": response,
        "quality_gate_passed": gate_passed,
        "attempts": [{"acceptance_decision": "accept", "tier": "local"}],
    }
    return json.dumps({"result": {"terminal_payload": {"payload": payload}}})


def test_local_path_reads_the_json_receipt_and_asks_for_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Plain-text output carries no receipt line: the answer was discarded and scored refused."""
    monkeypatch.setenv("OMNI_HOME", str(tmp_path))
    seen: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.append(list(argv))
        out = (
            _terminal_receipt("held value", gate_passed=True)
            if "--json" in argv
            else "held value\n"
        )
        return subprocess.CompletedProcess(argv, 0, out, "")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    got = runner.invoke_local(_review_bundle(), tmp_path / "onex", None)
    assert "--json" in seen[0]
    assert got.response == "held value"
    assert not got.refused
    assert got.quality_gate_passed is True
