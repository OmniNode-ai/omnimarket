# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The real subprocess boundary: one command, a time limit, and an outcome that never raises (OMN-24355)."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Sequence

from omnimarket.nodes.node_landing_decision_venv_effect.models.model_landing_decision_venv import (
    ModelCommandOutcome,
)

# Interpreter-location variables of the caller; a venv's interpreter must resolve its own packages.
INTERPRETER_ENV = ("PYTHONPATH", "PYTHONHOME")


class AdapterSubprocessRunner:
    def run(self, argv: Sequence[str], timeout_seconds: float) -> ModelCommandOutcome:
        env = {k: v for k, v in os.environ.items() if k not in INTERPRETER_ENV}
        try:
            done = subprocess.run(
                list(argv),
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
                env=env,
            )
        except subprocess.TimeoutExpired:
            return ModelCommandOutcome(returncode=-1, timed_out=True)
        except OSError as exc:
            return ModelCommandOutcome(returncode=-1, os_error=type(exc).__name__)
        return ModelCommandOutcome(returncode=done.returncode, stdout=done.stdout)
