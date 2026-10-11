# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The five skill-contract rules, keyed by the rule ids the checks report."""

from enum import StrEnum


class EnumSkillContractCheck(StrEnum):
    ARGS_PARITY = "args-parity"
    SUB_SKILL_EXISTS = "sub-skill-exists"
    SUB_SKILL_ARGS = "sub-skill-args"
    DUPLICATE_FRONTMATTER = "duplicate-frontmatter"
    SPEC_PROMPT_PREDICATES = "spec-prompt-predicates"
