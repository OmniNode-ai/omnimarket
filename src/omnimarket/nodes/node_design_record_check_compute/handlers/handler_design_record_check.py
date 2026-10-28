# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""R9: a design record needs a live check and a failure-mode table (OMN-21460).

The checker reads the text only. A record is the block that holds the lines
`Intended state:`, `Live check command:` and `Live check failing reading:` and one
table whose header names a `mode`, a `severity` and a `detector` column.
"""

from __future__ import annotations

import re
from pathlib import Path

from omnimarket.nodes.contract_topics import contract_section
from omnimarket.nodes.node_design_record_check_compute.models.model_design_record_check import (
    EnumDesignRecordIssue,
    ModelDesignProcessConfig,
    ModelDesignRecordCheckRequest,
    ModelDesignRecordCheckResult,
    ModelDesignRecordIssue,
)

CONTRACT_PATH = Path(__file__).resolve().parent.parent / "contract.yaml"
_GAP = re.compile(r"\bGAP\b")
_EMPTY_DETECTOR = {"", "-", "--", "none", "n/a", "tbd", "todo"}


def load_design_process_config(path: Path = CONTRACT_PATH) -> ModelDesignProcessConfig:
    """Read `config.design_process` of a contract; fail closed when it is absent."""
    return ModelDesignProcessConfig.model_validate(
        contract_section(path, "config", "design_process")
    )


def _field(text: str, key: str) -> str:
    pattern = re.compile(
        rf"^[ \t]*(?:[-*][ \t]+)?\**{key}\**[ \t]*:\**[ \t]*(.*)$",
        re.IGNORECASE | re.MULTILINE,
    )
    match = pattern.search(text)
    return match.group(1).strip().strip("`*").strip() if match else ""


def _cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _is_separator(line: str) -> bool:
    return bool(re.fullmatch(r"[\s|:\-]+", line)) and "-" in line


def _find_table(lines: list[str]) -> tuple[list[str], list[list[str]]] | None:
    for index, line in enumerate(lines):
        if not line.lstrip().startswith("|"):
            continue
        header = [cell.lower() for cell in _cells(line)]
        if not (
            any("mode" in cell for cell in header)
            and any("severity" in cell for cell in header)
            and any("detector" in cell for cell in header)
        ):
            continue
        rows: list[list[str]] = []
        for body in lines[index + 1 :]:
            if not body.lstrip().startswith("|"):
                break
            if _is_separator(body):
                continue
            rows.append(_cells(body))
        return header, rows
    return None


class HandlerDesignRecordCheck:
    """Refuse a plan or ticket body whose design record is missing a part."""

    def __init__(self, contract_path: Path = CONTRACT_PATH) -> None:
        self._config = load_design_process_config(contract_path)

    def handle(
        self, request: ModelDesignRecordCheckRequest
    ) -> ModelDesignRecordCheckResult:
        text = request.text
        lines = text.splitlines()
        issues: list[ModelDesignRecordIssue] = []

        def add(
            code: EnumDesignRecordIssue, part: str, message: str, row: int | None = None
        ) -> None:
            issues.append(
                ModelDesignRecordIssue(code=code, part=part, message=message, row=row)
            )

        command = _field(text, "live check command")
        failing = _field(text, "live check failing reading")
        intended = _field(text, "intended state")
        table = _find_table(lines)

        if not (command or failing or intended or table):
            add(
                EnumDesignRecordIssue.RECORD_MISSING,
                "design record",
                "no design record found: no intended state, live check or failure-mode table",
            )
        else:
            if not intended:
                add(
                    EnumDesignRecordIssue.INTENDED_STATE_MISSING,
                    "intended state",
                    "the record has no 'Intended state:' sentence",
                )
            if not command:
                add(
                    EnumDesignRecordIssue.LIVE_CHECK_COMMAND_MISSING,
                    "live check command",
                    "the record has no 'Live check command:' line",
                )

        rows: list[list[str]] = []
        uncovered = 0
        if table is None:
            if intended or command or failing:
                add(
                    EnumDesignRecordIssue.FAILURE_MODE_TABLE_MISSING,
                    "failure-mode table",
                    "the record has no failure-mode table (a table with mode, severity and detector columns)",
                )
        else:
            header, rows = table
            if not rows:
                add(
                    EnumDesignRecordIssue.FAILURE_MODE_TABLE_EMPTY,
                    "failure-mode table",
                    "the failure-mode table has no rows",
                )
            sev_col = next(i for i, c in enumerate(header) if "severity" in c)
            det_col = max(i for i, c in enumerate(header) if "detector" in c)
            for number, cells in enumerate(rows, start=1):
                detector = cells[det_col] if det_col < len(cells) else ""
                if detector.strip("`* ").lower() in _EMPTY_DETECTOR and not _GAP.search(
                    " ".join(cells)
                ):
                    uncovered += 1
                    add(
                        EnumDesignRecordIssue.ROW_WITHOUT_DETECTOR_OR_GAP,
                        "failure-mode table",
                        f"row {number} has no detector and no GAP mark",
                        number,
                    )
                severity = cells[sev_col].strip("`* ") if sev_col < len(cells) else ""
                if (
                    not severity.isdigit()
                    or int(severity) not in self._config.severity_levels
                ):
                    levels = ", ".join(str(v) for v in self._config.severity_levels)
                    add(
                        EnumDesignRecordIssue.SEVERITY_OUT_OF_RANGE,
                        "failure-mode table",
                        f"row {number} severity {severity!r} is not one of {levels}",
                        number,
                    )

        return ModelDesignRecordCheckResult(
            checked_at=request.now,
            issues=tuple(issues),
            exit_code=1 if issues else 0,
            record_issue_count=len(issues),
            failure_mode_rows=len(rows),
            rows_without_detector_or_gap=uncovered,
            live_check_command_present=bool(command),
            live_check_failing_reading=bool(failing),
        )
