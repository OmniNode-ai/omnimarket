# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Give a fixture task-class contract the transition lists every class declares.

OMN-21213: a task class without its ``retry_on``, ``move_on``, ``skip_on`` and
``terminate_on`` lists refuses to load. A fixture contract written for another
behaviour takes the packaged contract's ``transitions_default`` on each class
that does not declare its own.
"""

from __future__ import annotations

from typing import Any

import yaml

from omnimarket.routing.task_class_contract_path import (
    TASK_CLASS_CONTRACT_PACKAGED_DEFAULT_PATH,
)

DEFAULT_TRANSITIONS: dict[str, list[str]] = yaml.safe_load(
    TASK_CLASS_CONTRACT_PACKAGED_DEFAULT_PATH.read_text(encoding="utf-8")
)["transitions_default"]


def with_declared_transitions(contract: str) -> str:
    """``contract`` (YAML text) with the default lists on every class lacking them."""
    raw: dict[str, Any] = yaml.safe_load(contract)
    for entry in (raw.get("task_classes") or {}).values():
        if isinstance(entry, dict):
            entry.setdefault("transitions", DEFAULT_TRANSITIONS)
    return yaml.safe_dump(raw, sort_keys=False)


__all__ = ["DEFAULT_TRANSITIONS", "with_declared_transitions"]
