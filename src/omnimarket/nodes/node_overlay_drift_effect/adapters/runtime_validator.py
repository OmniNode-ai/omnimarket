# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Validators: in this process (the in-memory default) or in a runtime's own interpreter."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

from pydantic import ValidationError

from omnimarket.nodes.node_overlay_drift_effect import validate_program
from omnimarket.nodes.node_overlay_drift_effect.models.model_overlay_drift import (
    ModelOverlayPin,
    ModelRuntimeValidation,
)
from omnimarket.nodes.node_overlay_drift_effect.protocols import RuntimeValidatorError

PROGRAM = Path(validate_program.__file__).resolve()

Runner = Callable[[Sequence[str], str, int], "subprocess.CompletedProcess[str]"]


def _request(entries: tuple[tuple[ModelOverlayPin, str], ...]) -> dict[str, object]:
    return {
        "entries": [
            {"overlay_id": pin.overlay_id, "model": pin.model, "text": text}
            for pin, text in entries
        ]
    }


def _typed(answer: object) -> ModelRuntimeValidation:
    try:
        return ModelRuntimeValidation.model_validate(answer)
    except ValidationError:
        raise RuntimeValidatorError("the runtime's answer is not typed") from None


class InProcessRuntimeValidator:
    """Validate with the models this interpreter has installed (the in-memory default)."""

    def validate(
        self, entries: tuple[tuple[ModelOverlayPin, str], ...]
    ) -> ModelRuntimeValidation:
        return _typed(validate_program.run(_request(entries)))


def run_program(
    command: Sequence[str], stdin: str, timeout: int
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )


class InterpreterRuntimeValidator:
    """Validate in a runtime's own interpreter: the models that runtime has installed."""

    def __init__(
        self, python: Path, timeout_seconds: int, run: Runner = run_program
    ) -> None:
        self.python = python
        self.timeout_seconds = timeout_seconds
        self.run = run

    def validate(
        self, entries: tuple[tuple[ModelOverlayPin, str], ...]
    ) -> ModelRuntimeValidation:
        if not self.python.is_file():
            raise RuntimeValidatorError("runtime-interpreter-absent")
        try:
            done = self.run(
                [str(self.python), "-I", str(PROGRAM)],
                json.dumps(_request(entries)),
                self.timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeValidatorError("runtime-timeout") from None
        except OSError as exc:
            raise RuntimeValidatorError(
                f"runtime-unstartable-{type(exc).__name__}"
            ) from None
        if done.returncode:
            raise RuntimeValidatorError(f"runtime-exit-{done.returncode}")
        try:
            answer = json.loads(done.stdout)
        except ValueError:
            raise RuntimeValidatorError("runtime-answer-not-json") from None
        return _typed(answer)


__all__ = [
    "PROGRAM",
    "InProcessRuntimeValidator",
    "InterpreterRuntimeValidator",
    "run_program",
]
