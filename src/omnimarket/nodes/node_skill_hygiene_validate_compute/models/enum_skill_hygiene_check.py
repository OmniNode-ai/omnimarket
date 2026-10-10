# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The seven skill-hygiene rules, keyed by the rule ids the checks report."""

from enum import StrEnum


class EnumSkillHygieneCheck(StrEnum):
    UNDERSCORE_NAMES = "underscore-names"
    NO_DUPLICATE_NAMES = "no-duplicate-names"
    NO_UNINDEXED_NESTING = "no-unindexed-nesting"
    SKILL_MD_REQUIRED = "skill-md-required"
    NAME_MATCHES_DIR = "name-matches-dir"
    NO_PYTHON_IN_SKILLS = "no-python-in-skills"
    NO_ORPHAN_TOPICS = "no-orphan-topics"
