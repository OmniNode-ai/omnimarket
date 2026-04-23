# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""
Shared mypy parser for the ADK evaluation spike.

Parser path choice: this module uses ``mypy --output json`` exclusively.
Probed 2026-04-23 on mypy 1.x shipped with omnibase_core; JSON output works
and emits one JSON object per line (JSONL). We do NOT maintain a regex
fallback — the plan calls for exactly one parser path.

Task P4 of docs/plans/2026-04-23-adk-evaluation-tech-debt-agent.md.
"""

from __future__ import annotations

import json
import subprocess
from enum import StrEnum, unique
from pathlib import Path

from omnibase_core.enums.enum_lint_severity import EnumLintSeverity
from omnibase_core.models.quality.model_mypy_finding import ModelMypyFinding

MYPY_TIMEOUT_SECONDS = 120


@unique
class EnumDebtCategory(StrEnum):
    """Debt categories recognized by the category tagger."""

    ANY_USAGE = "any_usage"
    MISSING_RETURN = "missing_return"
    MISSING_ANNOTATION = "missing_annotation"
    DICT_ANY = "dict_any"


def parse_mypy_jsonl(text: str) -> list[ModelMypyFinding]:
    """Parse JSONL emitted by ``mypy --output json`` into sorted findings."""
    findings: list[ModelMypyFinding] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as e:
            msg = f"Malformed mypy JSON line: {raw_line!r}"
            raise ValueError(msg) from e
        if not isinstance(record, dict):
            msg = f"Expected JSON object per line, got: {type(record).__name__}"
            raise ValueError(msg)
        column_raw = record.get("column")
        column: int | None
        if column_raw is None or (isinstance(column_raw, int) and column_raw < 0):
            column = None
        elif isinstance(column_raw, int):
            column = column_raw
        else:
            msg = f"Non-integer column in mypy record: {column_raw!r}"
            raise ValueError(msg)

        severity_raw = record.get("severity", "error")
        try:
            severity = EnumLintSeverity(severity_raw)
        except ValueError as e:
            msg = f"Unknown mypy severity: {severity_raw!r}"
            raise ValueError(msg) from e

        code_raw = record.get("code")
        error_code: str
        if isinstance(code_raw, str) and code_raw:
            error_code = code_raw
        elif severity == EnumLintSeverity.NOTE:
            error_code = "note"
        else:
            error_code = "unknown"

        file_raw = record.get("file")
        line_raw = record.get("line")
        message_raw = record.get("message")
        if not isinstance(file_raw, str) or not file_raw:
            msg = f"Missing 'file' in mypy record: {record!r}"
            raise ValueError(msg)
        if not isinstance(line_raw, int) or line_raw < 1:
            msg = f"Invalid 'line' in mypy record: {line_raw!r}"
            raise ValueError(msg)
        if not isinstance(message_raw, str) or not message_raw:
            msg = f"Missing 'message' in mypy record: {record!r}"
            raise ValueError(msg)

        findings.append(
            ModelMypyFinding(
                file=file_raw,
                line=line_raw,
                column=column,
                severity=severity,
                error_code=error_code,
                message=message_raw,
            )
        )
    findings.sort(key=lambda f: (f.file, f.line, f.column or 0))
    return findings


def run_mypy_and_parse(repo_path: Path, target: str = "src/") -> list[ModelMypyFinding]:
    """Run ``uv run mypy <target> --strict --output json`` in ``repo_path`` and parse output.

    Raises:
        TimeoutError: if mypy does not finish within ``MYPY_TIMEOUT_SECONDS``.
        RuntimeError: if the subprocess fails to launch.
    """
    cmd = ["uv", "run", "mypy", target, "--strict", "--output", "json"]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=MYPY_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        msg = f"mypy exceeded {MYPY_TIMEOUT_SECONDS}s in {repo_path}"
        raise TimeoutError(msg) from e
    except FileNotFoundError as e:
        msg = f"Failed to launch mypy subprocess: {e!r}"
        raise RuntimeError(msg) from e
    # mypy exits non-zero on findings; that is expected. Parse stdout regardless.
    return parse_mypy_jsonl(proc.stdout)


def categorize(finding: ModelMypyFinding) -> EnumDebtCategory | None:
    """Tag a finding with a debt category, or return None if uncategorized.

    Pure function; no I/O. Unit-testable independently of mypy execution.
    """
    code = finding.error_code
    msg = finding.message
    lower = msg.lower()

    if "dict[str, any]" in lower:
        return EnumDebtCategory.DICT_ANY
    if code == "no-any-return" or "returning any" in lower:
        return EnumDebtCategory.ANY_USAGE
    if code == "no-untyped-def":
        if "return type annotation" in lower:
            return EnumDebtCategory.MISSING_RETURN
        if "missing a type annotation" in lower:
            return EnumDebtCategory.MISSING_ANNOTATION
    return None


__all__ = [
    "MYPY_TIMEOUT_SECONDS",
    "EnumDebtCategory",
    "categorize",
    "parse_mypy_jsonl",
    "run_mypy_and_parse",
]
