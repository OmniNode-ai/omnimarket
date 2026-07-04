# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Escalating-complexity graded ladder benchmark (OMN-13935, operator plan §3.6).

The operator decision-of-record re-cut the delegation benchmark from a smoke
test into a *graded* benchmark: an escalating-complexity corpus is run across the
EXISTING local delegation ladder rungs (including the 5090/4090 AI-PC rungs), and
the acceptance criterion is that the benchmark SEPARATES the rungs — the floor
rung scores measurably below the ceiling rung. Paid-cloud ceiling is deferred.

This package holds the deterministic scoring core:

* ``models``  — typed rung / task / result DTOs.
* ``graders`` — objective, deterministic graders (numeric, exact, sequence,
  code-execution) plus reasoning-model answer extraction.
* ``harness`` — load rungs + corpus + recorded rung outputs, grade every
  (rung, task) cell, roll up per-rung graded scores, and evaluate the
  floor < ceiling separation acceptance criterion.

The harness is pure and deterministic: it grades RECORDED rung outputs (durable
fixtures captured from the real local endpoints), never live model calls. Live
capture is a separate, explicitly-invoked recorder (``scripts/ci``) so CI stays
hermetic while the recorded evidence remains genuine per-rung model output.
"""

from omnimarket.delegation.graded_ladder.harness import (
    GradedLadderHarness,
    build_benchmark_packet,
)
from omnimarket.delegation.graded_ladder.models import (
    EnumBenchmarkTier,
    EnumGraderKind,
    ModelBenchmarkPacket,
    ModelGradedCell,
    ModelLadderRung,
    ModelLadderTask,
    ModelRungScore,
    ModelSeparationVerdict,
)

__all__ = [
    "EnumBenchmarkTier",
    "EnumGraderKind",
    "GradedLadderHarness",
    "ModelBenchmarkPacket",
    "ModelGradedCell",
    "ModelLadderRung",
    "ModelLadderTask",
    "ModelRungScore",
    "ModelSeparationVerdict",
    "build_benchmark_packet",
]
