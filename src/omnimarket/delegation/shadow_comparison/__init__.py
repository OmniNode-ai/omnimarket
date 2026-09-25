# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shadow-comparison harness (unified plan row G3): real delegation prompts,
answered by two rungs, graded by one grader, compared with an exact test and a
paired bootstrap interval."""

from omnimarket.delegation.shadow_comparison.harness import (
    ShadowGrader,
    ShadowJudge,
    ShadowRung,
    grade_like_the_local_path,
    read_shadow_prompts,
    run_shadow_comparison,
)
from omnimarket.delegation.shadow_comparison.models import (
    ModelShadowPrompt,
    ModelShadowRungAnswer,
)

__all__ = [
    "ModelShadowPrompt",
    "ModelShadowRungAnswer",
    "ShadowGrader",
    "ShadowJudge",
    "ShadowRung",
    "grade_like_the_local_path",
    "read_shadow_prompts",
    "run_shadow_comparison",
]
