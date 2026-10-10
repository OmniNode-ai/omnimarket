# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The two outbound ports of node_operator_capture_effect, and their production forms.

* The delegate port classifies one message through ``onex delegate`` with a typed response
  contract. Delegation is the only model path: no endpoint, key or model name is read here; the
  routing authority picks the rung and the receipt names it. The bus lane it delegates on is a
  deployment fact the caller passes (the serve process's own ``--bus-lane``); nothing here
  names one.
* The ledger port appends one row through the ledger's own append command, so every row passes
  the ledger's lock and guards. On the ledger host the work-ledger serve process lends its own
  append runner (:func:`runner_ledger_appender`). The one-shot ``process`` command reads
  ``ONEX_OPERATOR_CAPTURE_LEDGER_BIN`` (the command) and ``ONEX_LEDGER_PATH`` (the ledger); with
  either unset the append fails and the prompt stays in the inbox for the next run, never
  dropped.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from omnimarket.nodes.node_work_ledger_append_effect.protocols import (
    ProtocolLedgerAppendRunner,
)

ONEX_BIN_ENV = "ONEX_OPERATOR_CAPTURE_ONEX_BIN"
LEDGER_BIN_ENV = "ONEX_OPERATOR_CAPTURE_LEDGER_BIN"
LEDGER_PATH_ENV = "ONEX_LEDGER_PATH"
DELEGATE_TIMEOUT_S = 330
DELEGATE_TASK_CLASS = "summarization"


@dataclass(frozen=True)
class DelegateAnswer:
    """The model's raw answer and its attribution, or the reason there is none."""

    response: str | None
    model: str | None
    error: str | None


DelegateRunner = Callable[[str, Mapping[str, Any]], DelegateAnswer]
LedgerAppender = Callable[[str], tuple[bool, str]]


def _one_line(text: str, limit: int = 300) -> str:
    return " ".join(text.split())[-limit:]


def _terminal_payload(receipt: Mapping[str, Any]) -> Mapping[str, Any]:
    result = receipt.get("result")
    if not isinstance(result, Mapping):
        return {}
    terminal = result.get("terminal_payload")
    if isinstance(terminal, Mapping) and isinstance(terminal.get("payload"), Mapping):
        payload: Mapping[str, Any] = terminal["payload"]
        return payload
    return result


def onex_delegate_runner(
    lane: str | None,
    env: Mapping[str, str] | None = None,
) -> DelegateRunner:
    """The production delegate port: one ``onex delegate --json`` call per message.

    With no lane, every message is answered with an error, so the deterministic fallback
    classifies it: a capture is never lost to a missing deployment fact.
    """
    environ = dict(os.environ if env is None else env)

    def run(prompt: str, contract: Mapping[str, Any]) -> DelegateAnswer:
        if not lane:
            return DelegateAnswer(None, None, "no delegation lane configured")
        binary = environ.get(ONEX_BIN_ENV, "").strip() or shutil.which("onex")
        if not binary:
            return DelegateAnswer(
                None, None, f"no onex CLI ({ONEX_BIN_ENV} unset, none on PATH)"
            )
        with tempfile.TemporaryDirectory(prefix="operator-capture-") as tmp:
            prompt_file = Path(tmp) / "prompt.txt"
            contract_file = Path(tmp) / "contract.json"
            prompt_file.write_text(prompt, encoding="utf-8")
            contract_file.write_text(json.dumps(contract), encoding="utf-8")
            try:
                done = subprocess.run(
                    [
                        binary,
                        "delegate",
                        "--prompt-file",
                        str(prompt_file),
                        "--response-contract",
                        str(contract_file),
                        "--task-type",
                        DELEGATE_TASK_CLASS,
                        "--bus",
                        "kafka",
                        "--lane",
                        lane,
                        "--locus",
                        "deployed-lane",
                        "--json",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=DELEGATE_TIMEOUT_S,
                    check=False,
                    env=environ,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                return DelegateAnswer(
                    None, None, f"onex delegate did not finish: {exc}"
                )
        if done.returncode != 0:
            return DelegateAnswer(
                None,
                None,
                f"onex delegate exit {done.returncode}: {_one_line(done.stderr)}",
            )
        lines = [line for line in done.stdout.splitlines() if line.strip()]
        try:
            receipt: object = json.loads(lines[-1]) if lines else None
        except ValueError:
            receipt = None
        if not isinstance(receipt, Mapping):
            return DelegateAnswer(None, None, "onex delegate printed no JSON receipt")
        payload = _terminal_payload(receipt)
        response = payload.get("response")
        model = payload.get("model_name") or payload.get("model")
        if not isinstance(response, str) or not response.strip():
            return DelegateAnswer(
                None, None, "the delegate receipt carries no response"
            )
        if not isinstance(model, str) or not model.strip():
            return DelegateAnswer(None, None, "the delegate receipt names no model")
        return DelegateAnswer(response, model, None)

    return run


def onex_ledger_appender(env: Mapping[str, str] | None = None) -> LedgerAppender:
    """The production ledger port: ``<ledger command> --append <row>`` under the ledger's lock."""
    environ = dict(os.environ if env is None else env)

    def append(row: str) -> tuple[bool, str]:
        binary = environ.get(LEDGER_BIN_ENV, "").strip()
        if not binary:
            return False, f"{LEDGER_BIN_ENV} is unset: no ledger append command"
        if not environ.get(LEDGER_PATH_ENV, "").strip():
            return False, f"{LEDGER_PATH_ENV} is unset: no ledger to append to"
        try:
            done = subprocess.run(
                [binary, "--append", row],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
                env=environ,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, f"ledger append did not run: {exc}"
        if done.returncode != 0:
            return (
                False,
                f"ledger append exit {done.returncode}: {_one_line(done.stderr + done.stdout)}",
            )
        return True, _one_line(done.stdout, 120)

    return append


def runner_ledger_appender(runner: ProtocolLedgerAppendRunner) -> LedgerAppender:
    """The ledger port over the ledger host's own append runner (one row per call)."""

    def append(row: str) -> tuple[bool, str]:
        result = runner.append(row)
        if result.exit_code != 0:
            return (
                False,
                f"ledger append exit {result.exit_code}: "
                f"{_one_line(result.stderr + result.stdout)}",
            )
        return True, _one_line(result.stdout, 120)

    return append


__all__ = [
    "DELEGATE_TASK_CLASS",
    "LEDGER_BIN_ENV",
    "LEDGER_PATH_ENV",
    "ONEX_BIN_ENV",
    "DelegateAnswer",
    "DelegateRunner",
    "LedgerAppender",
    "onex_delegate_runner",
    "onex_ledger_appender",
    "runner_ledger_appender",
]
