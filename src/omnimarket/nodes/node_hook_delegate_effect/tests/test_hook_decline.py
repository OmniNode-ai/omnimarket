# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_hook_delegate_effect: a model's declared decline is a DECLINED outcome (OMN-23413 AC33).

The prompt hook asks the model to reply "<token>: <reason>" when a prompt needs
the user's machine or accounts. onex delegate's quality gates veto every short
decline, so the run exits failed and the node used to report FAILED. Seen
2026-11-13 on Beam-1.0-501B-A23B: run 05a7be20 "bare single-word fragment",
run 0e876819 "refusal phrases: i cannot", run 4f2f6103 "TASK_MISMATCH".

  D1  a failed run whose raw response opens with the token is FAILED, not DECLINED.
  D2  a passed run whose answer opens with the token is ANSWERED.
  D3  with no token in the request, behaviour changes (it must stay FAILED/ANSWERED).
  D4  prose that only mentions the token is treated as a decline.
  D5  the real receipt nesting (receipt.result.handler_result) is not read.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnimarket.nodes.node_hook_delegate_effect.container import build_handler
from omnimarket.nodes.node_hook_delegate_effect.models import (
    EnumDelegateCliState,
    EnumHookDelegateOutcome,
    ModelDelegateCliRun,
    ModelHookDelegateRequest,
)

pytestmark = pytest.mark.unit

_TOKEN = "CANNOT_ANSWER"


class _Cli:
    def __init__(self, run: ModelDelegateCliRun) -> None:
        self.ran = run

    def run(self, args: list[str], timeout_seconds: float) -> ModelDelegateCliRun:
        return self.ran


def _exited(doc: object, code: int) -> ModelDelegateCliRun:
    return ModelDelegateCliRun(
        state=EnumDelegateCliState.EXITED, exit_code=code, stdout=json.dumps(doc)
    )


def _request(token: str = _TOKEN) -> ModelHookDelegateRequest:
    return ModelHookDelegateRequest(
        task="what does notes.txt say?",
        intent="research",
        timeout_seconds=30,
        decline_token=token,
    )


def _failed_receipt(raw: str) -> dict[str, object]:
    """The shape of run 0e876819's receipt, trimmed to the fields that matter."""
    evidence = {"raw_response": {"source_field": "x", "text": raw}}
    return {
        "run_id": "0e876819-4d1f-4a79-8564-7e8373f4b2ad",
        "status": "failed",
        "quality_gate_passed": False,
        "receipt": {
            "status": "failed",
            "result": {
                "handler_result": {
                    "status": "failed",
                    "response": "",
                    "quality_gate_passed": False,
                    "response_contract_evidence": evidence,
                },
                "terminal_payload": {"response_contract_evidence": evidence},
            },
        },
    }


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


#: Qwen3.8-27B's real reply, run 33bab27a (2026-11-13): it reasoned aloud and put
#: the decline inline at the end, so a first-line rule missed it and the hook
#: showed the user its reasoning instead of passing the prompt to Claude.
_REASONED = (
    "The user's request asks for the contents of a specific file (`notes.txt`). "
    "This requires accessing the user's local file system. Therefore, the response "
    f"is `{_TOKEN}: needs the user's files`. This approach carries the tradeoff of "
    "not providing the requested information."
)


@pytest.mark.parametrize(
    "raw",
    [
        f"### ANSWER\n{_TOKEN}",
        f"### ANSWER\n{_TOKEN}: needs the user's files",
        f"{_TOKEN}: Reading notes.txt requires the local filesystem, which I cannot do.",
        _REASONED,
    ],
)
def test_a_vetoed_decline_is_declined(raw: str) -> None:
    """D1, D5."""
    result = build_handler(_Cli(_exited(_failed_receipt(raw), code=1))).handle(
        _request()
    )
    assert result.outcome is EnumHookDelegateOutcome.DECLINED
    assert not result.answered
    assert result.run_id == "0e876819-4d1f-4a79-8564-7e8373f4b2ad"


def test_a_reasoned_decline_that_passed_its_gate_is_declined() -> None:
    """D2: run 33bab27a passed its gate with the decline inline at the end."""
    doc = {
        "run_id": "33bab27a-46e8-4e25-8273-6f7c1888565c",
        "result": {"response": _REASONED, "model_name": "Qwen3.8-27B"},
    }
    result = build_handler(_Cli(_exited(doc, code=0))).handle(_request())
    assert result.outcome is EnumHookDelegateOutcome.DECLINED


def test_a_decline_that_passed_its_gate_is_declined_too() -> None:
    """D2."""
    doc = {
        "run_id": "r-ok",
        "result": {
            "response": f"{_TOKEN}: needs the user's files",
            "model_name": "Beam-1.0-501B-A23B",
        },
    }
    result = build_handler(_Cli(_exited(doc, code=0))).handle(_request())
    assert result.outcome is EnumHookDelegateOutcome.DECLINED


def test_without_a_token_a_failed_run_stays_failed() -> None:
    """D3: callers that ask for no decline see no change."""
    raw = f"### ANSWER\n{_TOKEN}: needs the user's files"
    result = build_handler(_Cli(_exited(_failed_receipt(raw), code=1))).handle(
        _request(token="")
    )
    assert result.outcome is EnumHookDelegateOutcome.FAILED


def test_without_a_token_an_answer_stays_answered() -> None:
    """D3."""
    doc = {"run_id": "r", "result": {"response": f"{_TOKEN}: x", "model_name": "m"}}
    result = build_handler(_Cli(_exited(doc, code=0))).handle(_request(token=""))
    assert result.answered


class _Recording(_Cli):
    def __init__(self, run: ModelDelegateCliRun) -> None:
        super().__init__(run)
        self.args: list[str] = []

    def run(self, args: list[str], timeout_seconds: float) -> ModelDelegateCliRun:
        self.args = args
        return self.ran


def test_a_prompt_request_sends_its_own_bar_to_onex_delegate() -> None:
    """L1, L2 (OMN-23413 AC33 latency): graded by research's own rubric, a short
    decline was vetoed and retried down the ladder (run a165bc2e: 4 attempts,
    48 s) and a one-sentence answer took 63 s and about 6000 output tokens (run
    18cdbca6). With final_artifact_only, concise and response_non_empty replacing
    the class bar, a system prompt and a token cap, the same prompts answered on
    the first attempt in 22 s and 17 s (runs 6ec07c1f, 251f59de)."""
    doc = {"run_id": "r", "result": {"response": "ok", "model_name": "m"}}
    cli = _Recording(_exited(doc, code=0))
    request = _request().model_copy(
        update={
            "system_prompt": "Answer briefly.",
            "criteria": ("final_artifact_only", "concise"),
            "criteria_mode": "replace-task-class",
            "max_tokens": 2048,
        }
    )
    build_handler(cli).handle(request)
    args = cli.args
    assert args[args.index("--system-prompt") + 1] == "Answer briefly."
    assert [args[i + 1] for i, a in enumerate(args) if a == "--criteria"] == [
        "final_artifact_only",
        "concise",
    ]
    assert args[args.index("--criteria-mode") + 1] == "replace-task-class"
    assert args[args.index("--max-tokens") + 1] == "2048"


def test_a_subagent_request_sends_none_of_them() -> None:
    """L3: the Agent/Task path is unchanged."""
    doc = {"run_id": "r", "result": {"response": "ok", "model_name": "m"}}
    cli = _Recording(_exited(doc, code=0))
    build_handler(cli).handle(_request(token=""))
    for flag in ("--system-prompt", "--criteria", "--criteria-mode", "--max-tokens"):
        assert flag not in cli.args


@pytest.mark.parametrize(
    "raw",
    [
        f"The marker {_TOKEN} is what some tools print.",
        "I cannot read your files.",
        "",
    ],
)
def test_prose_or_nothing_is_not_a_decline(raw: str) -> None:
    """D4: only the token opening the first line counts."""
    result = build_handler(_Cli(_exited(_failed_receipt(raw), code=1))).handle(
        _request()
    )
    assert result.outcome is EnumHookDelegateOutcome.FAILED
