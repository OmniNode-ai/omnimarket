# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Track B POC for the ADK evaluation spike.

NOT a production node; does not register in the ``onex.nodes`` entry-point
group, does not ship a ``contract.yaml``, and does not declare Kafka
topics. This package exists only to exercise the real AdapterModelRouter
code path against local Qwen3-Coder at :port:`8000` on .201, so the
evaluation reads apples-to-apples against Track A (ADK + Gemini).

See ``docs/plans/2026-04-23-adk-evaluation-tech-debt-agent.md`` for the
decision gate and the "Evaluation Truth Boundary" constraints.
"""

from omnimarket.experiments.adk_eval.type_debt_scout_poc.handler_type_debt_scout import (
    ModelTrackBConfig,
    run_type_debt_scout,
)

__all__ = [
    "ModelTrackBConfig",
    "run_type_debt_scout",
]
