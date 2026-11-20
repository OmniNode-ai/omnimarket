# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One tick of the second benchmark arm: send one pinned delegate run and read who answered (OMN-25052).

The delegation ladder's second local arm pins the backend id the dev lane overlay declares for the second lab
server. This module sends one run with that pin through the ``onex delegate`` command, the same command shape the
ladder runner builds, and reads the terminal receipt it prints. A run counts only when it completed and the last
attempt names the pinned backend: a run another rung answered, a refusal, a timeout or output with no receipt is a
failed outcome that names it, never a silent skip.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Mapping, Sequence
from typing import NamedTuple

CommandRunner = Callable[[Sequence[str], int], tuple[int, str, str]]


class ModelSecondArmTickOutcome(NamedTuple):
    """How one scheduled run went: failed, and the detail the run event carries."""

    failed: bool
    detail: str


def run_command(argv: Sequence[str], timeout_seconds: int) -> tuple[int, str, str]:
    """Run the delegate command with no shell; a missing binary is exit 127, a timeout exit 124."""
    try:
        done = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return 124, "", f"the delegate command exceeded {timeout_seconds}s"
    except OSError as exc:
        return 127, "", f"{type(exc).__name__}: {exc}"
    return done.returncode, done.stdout, done.stderr


def delegate_argv(
    *,
    delegate_command: Sequence[str],
    prompt: str,
    task_type: str,
    backend_id: str,
    transport_args: Sequence[str],
) -> list[str]:
    """The ladder runner's deployed-lane command shape, with the pin last."""
    return [
        *delegate_command,
        prompt,
        "--task-type",
        task_type,
        *transport_args,
        "--backend-id",
        backend_id,
        "--json",
    ]


def _receipt_line(stdout: str) -> Mapping[str, object] | None:
    line = next(
        (ln for ln in reversed(stdout.splitlines()) if ln.strip().startswith("{")),
        None,
    )
    if line is None:
        return None
    try:
        doc = json.loads(line)
    except ValueError:
        return None
    return doc if isinstance(doc, dict) else None


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, dict) else {}


def run_second_arm_tick(
    *,
    backend_id: str,
    task_type: str,
    prompt: str,
    delegate_command: Sequence[str],
    transport_args: Sequence[str],
    timeout_seconds: int,
    runner: CommandRunner = run_command,
) -> ModelSecondArmTickOutcome:
    """Send one run pinned on the arm's backend id and judge it from its terminal receipt."""
    argv = delegate_argv(
        delegate_command=delegate_command,
        prompt=prompt,
        task_type=task_type,
        backend_id=backend_id,
        transport_args=transport_args,
    )
    code, stdout, stderr = runner(argv, timeout_seconds)
    receipt = _receipt_line(stdout)
    if receipt is None:
        tail = (stderr.strip() or stdout.strip())[-200:]
        return ModelSecondArmTickOutcome(
            True, f"the delegate command exit={code} printed no receipt: {tail}"
        )
    payload = _mapping(
        _mapping(_mapping(receipt.get("result")).get("terminal_payload")).get("payload")
    )
    if not payload:
        return ModelSecondArmTickOutcome(
            True, f"the delegate receipt exit={code} has no terminal payload"
        )
    attempts = payload.get("attempts")
    last = _mapping(attempts[-1]) if isinstance(attempts, list) and attempts else {}
    answered = last.get("backend_id")
    status = payload.get("status")
    run_id = receipt.get("run_id")
    detail = (
        f"backend_id={answered} pinned={backend_id} status={status} "
        f"run_id={run_id} model={payload.get('model_name')}"
    )
    if status != "completed":
        return ModelSecondArmTickOutcome(True, detail)
    if answered != backend_id:
        return ModelSecondArmTickOutcome(True, f"{detail} the pin was not honoured")
    return ModelSecondArmTickOutcome(False, detail)


__all__: list[str] = [
    "CommandRunner",
    "ModelSecondArmTickOutcome",
    "delegate_argv",
    "run_command",
    "run_second_arm_tick",
]
