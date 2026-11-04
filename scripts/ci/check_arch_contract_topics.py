#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Validate contract topic declarations without interpreting prose as a topic."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

_TOPIC = re.compile(r"onex\.(cmd|evt|dlq|intent|snapshot)\.[a-z_-]+\.[a-z_-]+\.v[0-9]+")


def violations(value: object, path: str = "") -> list[str]:
    """Check named topic fields, including nested event-bus and routing entries."""
    found: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            field = f"{path}.{key}" if path else str(key)
            if isinstance(key, str) and (
                key in {"topic", "topics"} or key.endswith(("_topic", "_topics"))
            ):
                values = child if isinstance(child, list) else [child]
                for item in values:
                    if isinstance(item, str) and not _TOPIC.fullmatch(item):
                        found.append(f"{field}: {item}")
            found.extend(violations(child, field))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(violations(child, f"{path}[{index}]"))
    return found


def main(paths: list[str]) -> int:
    failed = False
    for name in paths:
        path = Path(name)
        # A removed contract is included in git diff --name-only.
        if not path.exists():
            continue
        try:
            value = yaml.safe_load(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError("contract must be a YAML mapping")
            problems = violations(value)
        except (OSError, ValueError, yaml.YAMLError) as exc:
            problems = [f"cannot read contract: {exc}"]
        for problem in problems:
            print(f"{name}: {problem}")
            failed = True
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
