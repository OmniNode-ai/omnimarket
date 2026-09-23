# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Parse a model's reply to the WRITE/REPAIR response contract (OMN-19362).

Pure. The reply should be one JSON object ``{test_path, test_source}``; local
models often wrap it in a code fence or add prose around it, so the parser
looks inside fences first, then at the whole text. A reply that yields no
compilable module is an unusable reply, with the reason stated, never a test.
"""

from __future__ import annotations

import json
import re

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def parse_test_reply(text: str, expected_path: str) -> tuple[str, str]:
    """(test_source, invalid_reason) from a model reply to the WRITE contract."""
    candidates = [m.group(1) for m in _FENCE.finditer(text)] + [text]
    for candidate in candidates:
        start, end = candidate.find("{"), candidate.rfind("}")
        if start < 0 or end <= start:
            continue
        try:
            data = json.loads(candidate[start : end + 1])
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        source = data.get("test_source")
        if not isinstance(source, str) or not source.strip():
            return "", "the JSON object has no test_source string"
        if data.get("test_path") not in (expected_path, None):
            return "", f"test_path {data.get('test_path')!r} is not {expected_path!r}"
        try:
            compile(source, expected_path, "exec")
        except SyntaxError as exc:
            return "", f"test_source does not compile: {exc.msg} (line {exc.lineno})"
        return source, ""
    return "", "the reply is not a JSON object with test_path and test_source"


__all__ = ["parse_test_reply"]
