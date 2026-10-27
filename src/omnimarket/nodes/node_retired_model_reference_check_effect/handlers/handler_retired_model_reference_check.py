# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Reject retired-model references, including comments and dated evidence.

Deployment model identifiers belong in external contract overlays. The evidence
repository exception does not apply to any file in this repository.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import yaml

from ..models.model_retired_model_reference_check import (
    ModelRetiredModelReferenceCheckRequest,
    ModelRetiredModelReferenceCheckResult,
)

RETIRED = re.compile(r"gpt[\s_-]*oss", re.IGNORECASE)


def _strings(node: object) -> list[str]:
    if isinstance(node, str):
        return [node]
    if isinstance(node, dict):
        return [
            s for key, value in node.items() for s in (*_strings(key), *_strings(value))
        ]
    if isinstance(node, list):
        return [s for item in node for s in _strings(item)]
    return []


def violations(root: Path, paths: list[Path]) -> list[str]:
    found = []
    for path in paths:
        if not path.is_file() or path.is_symlink():
            continue
        relative = str(path.relative_to(root))
        if RETIRED.search(relative):
            found.append(f"{relative}: retired-model reference in filename")
        contents = path.read_bytes().decode("utf-8", errors="replace")
        raw_matches = False
        for number, line in enumerate(contents.splitlines(), 1):
            if RETIRED.search(line):
                found.append(f"{relative}:{number}: retired-model reference")
                raw_matches = True
        # Keep the original parsed-YAML protection: escapes in keys and values
        # must not become a way around the stronger comment/history check.
        try:
            if path.suffix in {".yaml", ".yml"}:
                parsed = list(yaml.safe_load_all(contents))
            elif path.suffix == ".json":
                parsed = json.loads(contents)
            else:
                parsed = None
        except (yaml.YAMLError, json.JSONDecodeError):
            if relative.startswith("src/omnimarket/"):
                found.append(f"{relative}: cannot inspect invalid structured content")
            parsed = None
        if not raw_matches and any(RETIRED.search(s) for s in _strings(parsed)):
            found.append(f"{relative}: retired-model reference in parsed content")
    return found


class HandlerRetiredModelReferenceCheck:
    """Read repository content and refuse every retired-model reference."""

    def handle(
        self, request: ModelRetiredModelReferenceCheckRequest
    ) -> ModelRetiredModelReferenceCheckResult:
        root = Path(request.repo_root).resolve()
        if request.paths:
            paths = [root / path for path in request.paths]
        else:
            result = subprocess.run(
                ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                cwd=root,
                capture_output=True,
                check=True,
            )
            paths = [
                root / name.decode() for name in result.stdout.split(b"\0") if name
            ]
        found = violations(root, paths)
        return ModelRetiredModelReferenceCheckResult(
            admitted=not found, exit_code=int(bool(found)), output_lines=tuple(found)
        )
