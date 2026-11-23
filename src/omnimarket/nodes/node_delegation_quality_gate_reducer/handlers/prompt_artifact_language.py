# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The artifact language a delegated prompt asks for (OMN-26588)."""

from __future__ import annotations

import json
import re

import yaml

_PROMPT_PYTHON_RE = re.compile(r"\bpython3?\b|\bpytest\b|\.py\b", re.IGNORECASE)
_PROMPT_YAML_RE = re.compile(r"\bya?ml\b", re.IGNORECASE)
_PROMPT_JSON_RE = re.compile(r"\bjson\b", re.IGNORECASE)


def prompt_requested_artifact_language(task_prompt: str | None) -> str | None:
    """The artifact language the prompt asks for, when it names exactly one of YAML or JSON.

    OMN-26588. An answer with no fence, or a fence with no language tag, has no
    declared language of its own, so the prompt supplies it. A prompt that names
    Python, names both YAML and JSON, or names neither returns ``None`` and the
    answer keeps the Python parse it always had.
    """
    if not task_prompt or _PROMPT_PYTHON_RE.search(task_prompt):
        return None
    wants_yaml = _PROMPT_YAML_RE.search(task_prompt) is not None
    wants_json = _PROMPT_JSON_RE.search(task_prompt) is not None
    if wants_yaml == wants_json:
        return None
    return "yaml" if wants_yaml else "json"


def parse_as_requested_language(body: str, language: str) -> str | None:
    """Parse ``body`` as the prompt-requested YAML or JSON (OMN-26588)."""
    if language == "yaml":
        try:
            yaml.safe_load(body)
        except yaml.YAMLError as exc:
            return f"MALFORMED: response does not compile as YAML: {exc}"
        return None
    try:
        json.loads(body)
    except json.JSONDecodeError as exc:
        return f"MALFORMED: response does not compile as JSON: {exc.msg}"
    return None


__all__ = ["parse_as_requested_language", "prompt_requested_artifact_language"]
