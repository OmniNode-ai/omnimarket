# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The typed reading of a lab experiment report: its results tables, paired tests, verdict and task set.

``parse_report`` reads the report's markdown text into the ``ModelImportedExperiment`` objects the history compute folds. It
takes the text, not a file, so the parser does no I/O, and it never raises on a report it cannot read: what it cannot read
comes back as a list of problems, and a reading it cannot make is absent, never invented. ``split_remote_listing`` turns the
lab's file listing -- a ``@@FILE <name> <mtime>`` line followed by the file's text, block after block -- back into files.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict

from omnimarket.models.floor_delegation_history import (
    DateSource,
)
from omnimarket.models.floor_history_request import (
    ModelImportedArm,
    ModelImportedBaseline,
    ModelImportedExperiment,
    ModelImportedPaired,
)


class ReportFile(BaseModel):
    """One file of the lab's listing: its name, its mtime in epoch seconds, and its text."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    mtime_epoch: int
    text: str


# The correct/n of a results table row, e.g. "12 / 15" or "12/15"; the first number is the correct count.
_CORRECT = re.compile(r"(\d+)\s*/\s*(\d+)")
# The first number of a latency or cost cell; "none", "n/a" and the empty cell give no match.
_NUMBER = re.compile(r"\d+(?:\.\d+)?")
# A sha256: exactly 64 hex digits, not part of a longer run.
_SHA256 = re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{64}(?![0-9a-fA-F])")
# The report's title: a markdown heading line.
_HEADING = re.compile(r"^\s*#{1,6}\s+(?P<text>.+?)\s*$")
# A separator cell of a markdown table: dashes, optionally colon aligned.
_SEPARATOR_CELL = re.compile(r"^:?-+:?$")
# A paired test: "- <a> vs <b>: <x> vs <y>, p=<p>".
_PAIRED_PLAIN = re.compile(
    r"^\s*[-*]\s+(?P<a>.+?)\s+vs\s+(?P<b>.+?)\s*:\s*(?P<x>\d+)\s+vs\s+(?P<y>\d+)\s*,\s*"
    r"p\s*(?P<rel>[=<>])\s*(?P<pv>\d+(?:\.\d+)?)\s*$",
    re.IGNORECASE,
)
# A paired test that names itself: "- McNemar <a> vs <b>: only-<a> right <x>, only-<b> right <y>, p=<p>".
_PAIRED_MCNEMAR = re.compile(
    r"^\s*[-*]\s+mcnemar\s+(?P<a>.+?)\s+vs\s+(?P<b>.+?)\s*:\s*only-(?P<oa>.+?)\s+right\s+(?P<x>\d+)\s*,\s*"
    r"only-(?P<ob>.+?)\s+right\s+(?P<y>\d+)\s*,\s*p\s*(?P<rel>[=<>])\s*(?P<pv>\d+(?:\.\d+)?)\s*$",
    re.IGNORECASE,
)
# The prefixes a verdict line may begin with (checked case-insensitively).
_VERDICT_PREFIXES = ("verdict", "result:", "decision")
# A sentence that starts a verdict inside a paragraph.
_RESULT_SENTENCE = "Result:"
# The longest verdict line kept.
_VERDICT_LIMIT = 300
# A results table is the one whose first header cell starts with this word: its rows are the arms.
_ARM_HEADER = "arm"
# A column header that counts items rather than results, and the words that mark the result column.
_COUNT_HEADERS = ("n ", "n(", "n/")
_RESULT_HEADERS = ("correct", "pass", "accuracy", "exact", "recall")
# A header that names the p50 latency column.
_P50_HEADERS = ("p50",)
# A header that names the summed GPU-seconds column.
_GPU_HEADERS = ("summed", "gpu seconds", "sum s")
# The first line of one block of the lab's file listing.
_FILE_HEADER = "@@FILE "


def _cells(line: str) -> tuple[str, ...]:
    """The cells of a pipe row, the outer pipes and the padding removed."""
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return tuple(c.strip() for c in s.split("|"))


def _is_pipe_row(line: str) -> bool:
    """A line that may be a row of a markdown pipe table."""
    s = line.strip()
    return s.startswith("|") and s.count("|") >= 2


def _is_separator_row(line: str) -> bool:
    """The dashes-and-pipes row that follows the header of a markdown table."""
    s = line.strip()
    if "|" not in s:
        return False
    cells = [c for c in (part.strip() for part in s.split("|")) if c]
    return bool(cells) and all(_SEPARATOR_CELL.fullmatch(c) for c in cells)


def _find_tables(
    lines: list[str],
) -> tuple[list[tuple[int, int, str, list[str]]], set[int]]:
    """The pipe tables of the report, each with its header line, its data lines and its line span."""
    tables: list[tuple[int, int, str, list[str]]] = []
    table_lines: set[int] = set()
    i, n = 0, len(lines)
    while i < n:
        if i + 1 < n and _is_pipe_row(lines[i]) and _is_separator_row(lines[i + 1]):
            start = i
            end = start + 2
            while end < n and _is_pipe_row(lines[end]):
                end += 1
            if end > start + 2:
                table_lines.update(range(start, end))
                tables.append((start, end, lines[start], lines[start + 2 : end]))
            i = end
        else:
            i += 1
    return tables, table_lines


def _report_question(lines: list[str]) -> str:
    """The question of the report's first heading, or "" when the report has no heading."""
    for line in lines:
        m = _HEADING.match(line)
        if m is None:
            continue
        heading = m["text"]
        if ":" in heading:
            return heading.split(":", 1)[1].strip()
        parts = heading.split(None, 1)
        return parts[1].strip() if len(parts) > 1 else ""
    return ""


def _report_verdict(lines: list[str]) -> str | None:
    """The report's own concluding line, or None when it states none.

    In order: the first line under a ``Verdict`` heading; a line that starts with ``Verdict``, ``Result:`` or ``Decision`` or carries a
    ``Result:`` sentence (from that word to the end of the line); the first line under a ``Findings`` heading. List markers and emphasis
    are removed and the line is cut at ``_VERDICT_LIMIT`` characters. A report with none of them has no verdict.
    """
    verdict_heading = _first_line_under(lines, "verdict")
    if verdict_heading is not None:
        return verdict_heading
    for line in lines:
        s = _plain(line)
        if s.casefold().startswith(_VERDICT_PREFIXES):
            return s[:_VERDICT_LIMIT]
        at = s.find(_RESULT_SENTENCE)
        if at >= 0:
            return s[at : at + _VERDICT_LIMIT]
    return _first_line_under(lines, "findings")


def _plain(line: str) -> str:
    """A line without its list marker and emphasis."""
    s = line.strip()
    while True:
        s = s.lstrip()
        if s.startswith("**"):
            s = s[2:]
        elif s.startswith(("*", "-")):
            s = s[1:]
        else:
            break
    return re.sub(r"\*", "", s).strip()


def _first_line_under(lines: list[str], heading_word: str) -> str | None:
    """The first non-empty, non-table line after a heading whose text starts with ``heading_word``."""
    for i, line in enumerate(lines):
        m = _HEADING.match(line)
        if m is None or not m["text"].casefold().startswith(heading_word):
            continue
        for follow in lines[i + 1 :]:
            if not follow.strip():
                continue
            if _HEADING.match(follow) or _is_pipe_row(follow):
                break
            return _plain(follow)[:_VERDICT_LIMIT]
    return None


def _task_set(lines: list[str], hash_markers: Sequence[str]) -> tuple[str | None, str]:
    """The task set's sha256 and the line that names it, or None and "" when no line does."""
    for line in lines:
        low = line.casefold()
        if not any(marker and marker.casefold() in low for marker in hash_markers):
            continue
        m = _SHA256.search(line)
        if m is None:
            continue
        return m[0].lower(), line.strip()[:200]
    return None, ""


def _first_number(cell: str) -> float | None:
    """The first number of a cell as a float, or None when the cell names none."""
    m = _NUMBER.search(cell)
    if m is None:
        return None
    return float(m[0])


def _strip_label(cell: str) -> str:
    """An arm label without its backticks and asterisks."""
    return re.sub(r"[*`]", "", cell).strip()


def _clean_name(cell: str) -> str:
    """An arm name as written, without its backticks."""
    return cell.replace("`", "").strip()


def _column_named(header_cells: tuple[str, ...], names: tuple[str, ...]) -> int | None:
    """The first column whose header names one of ``names`` (case-insensitively), or None."""
    for ci, cell in enumerate(header_cells):
        if any(name in cell.casefold() for name in names):
            return ci
    return None


def _correct_column(header_cells: tuple[str, ...], rows: list[str]) -> int | None:
    """The column that gives each arm's correct/n count, or None when the table is not a results table.

    A candidate column has a correct/n cell in at least half the rows. A column whose header starts like a count of items (``n (pos/neg)``)
    is not a result; a header that names a result (``correct``, ``pass``, ``accuracy``, ``exact``, ``recall``) wins over an unnamed column.
    """
    cells_rows = [_cells(row) for row in rows]
    n = len(cells_rows)
    if n == 0:
        return None
    candidates = [
        ci
        for ci in range(len(header_cells))
        if not header_cells[ci].casefold().startswith(_COUNT_HEADERS)
        and sum(
            1 for cells in cells_rows if ci < len(cells) and _CORRECT.search(cells[ci])
        )
        * 2
        >= n
    ]
    for ci in candidates:
        if any(word in header_cells[ci].casefold() for word in _RESULT_HEADERS):
            return ci
    return candidates[0] if candidates else None


def _intro_line(lines: list[str], index: int) -> str | None:
    """The nearest line above ``index`` that is neither empty nor a list item: what introduces the list of paired tests."""
    for j in range(index - 1, -1, -1):
        s = lines[j].strip()
        if s and not s.startswith(("-", "*")):
            return s
    return None


def _paired_line(
    line: str, prev: str | None, line_no: int
) -> tuple[ModelImportedPaired | None, str | None]:
    """The paired test of a list item line, or None; a line that cannot be read is a problem."""
    m = _PAIRED_MCNEMAR.match(line) or _PAIRED_PLAIN.match(line)
    if m is None:
        return None, None
    if m["rel"] == "<":
        return None, (
            f"line {line_no}: p<{m['pv']!r} is a bound, not a value, and no value may be "
            "invented; the line is skipped"
        )
    p_value = float(m["pv"])
    if not 0.0 <= p_value <= 1.0:
        return None, (
            f"line {line_no}: the p-value {p_value} of the paired test is outside "
            "[0, 1]; the line is skipped"
        )
    named = "mcnemar" in line.casefold() or (
        prev is not None and "mcnemar" in prev.casefold()
    )
    return (
        ModelImportedPaired(
            test="exact McNemar" if named else "paired test",
            arm_a=_clean_name(m["a"]),
            arm_b=_clean_name(m["b"]),
            only_a_right=int(m["x"]),
            only_b_right=int(m["y"]),
            p_value=p_value,
        ),
        None,
    )


def _read_rows(
    table_no: int, header: str, rows: list[str], problems: list[str]
) -> tuple[tuple[ModelImportedArm, ...], ModelImportedBaseline | None] | None:
    """The arms and the baseline of one results table; None when the table is not a results table."""
    header_cells = _cells(header)
    if not header_cells or not header_cells[0].casefold().startswith(_ARM_HEADER):
        return None
    correct_col = _correct_column(header_cells, rows)
    if correct_col is None:
        return None
    p50_col = _column_named(header_cells, _P50_HEADERS)
    gpu_col = _column_named(header_cells, _GPU_HEADERS)
    arms: list[ModelImportedArm] = []
    baseline: ModelImportedBaseline | None = None
    for row in rows:
        cells = _cells(row)
        label = _strip_label(cells[0]) if cells else ""
        cell = cells[correct_col] if correct_col < len(cells) else ""
        m = _CORRECT.search(cell)
        if m is None:
            problems.append(
                f"table {table_no}: the row for {label!r} does not give a correct/n "
                "count; it is skipped"
            )
            continue
        correct, n = int(m[1]), int(m[2])
        if n <= 0 or correct > n:
            problems.append(
                f"table {table_no}: the row for {label!r} answers {correct}/{n}; "
                "correct must not exceed a positive n; it is skipped"
            )
            continue
        lf = label.casefold()
        if "majority" in lf or lf.startswith("baseline") or "always" in lf:
            if baseline is None:
                baseline = ModelImportedBaseline(label=label, correct=correct, n=n)
            continue
        arms.append(
            ModelImportedArm(
                label=label,
                correct=correct,
                n=n,
                p50_latency_s=(
                    _first_number(cells[p50_col])
                    if p50_col is not None and p50_col < len(cells)
                    else None
                ),
                gpu_seconds=(
                    _first_number(cells[gpu_col])
                    if gpu_col is not None and gpu_col < len(cells)
                    else None
                ),
            )
        )
    return tuple(arms), baseline


def _table_suffix(start: int, lines: list[str], table_lines: set[int]) -> str:
    """The "[section]" added to the question of a table that follows its section line."""
    j = start - 1
    while j >= 0 and (not lines[j].strip() or j in table_lines):
        j -= 1
    if j < 0:
        return ""
    s = lines[j].strip()
    if not (s.startswith("#") or s.startswith("**")):
        return ""
    cleaned = re.sub(r"[#*]", "", s).strip()
    return f" [{cleaned}]" if cleaned else ""


def _parse_report(
    text: str,
    *,
    report_id: str,
    source: str,
    date: str,
    date_source: DateSource,
    hash_markers: tuple[str, ...],
) -> tuple[tuple[ModelImportedExperiment, ...], tuple[str, ...]]:
    lines = text.splitlines()
    problems: list[str] = []
    question = _report_question(lines)
    verdict = _report_verdict(lines)
    task_set_hash, task_set_description = _task_set(lines, hash_markers)
    tables, table_lines = _find_tables(lines)
    experiments: list[ModelImportedExperiment] = []
    table_ends: list[int] = []
    paired: list[list[ModelImportedPaired]] = []
    table_no = 0
    k = 0
    for start, end, header, rows in tables:
        table_no += 1
        read = _read_rows(table_no, header, rows, problems)
        if read is None:
            continue
        arms, baseline = read
        if not arms:
            problems.append(
                f"table {table_no}: no arm rows, every row is a baseline; "
                "the table is dropped"
            )
            continue
        k += 1
        experiments.append(
            ModelImportedExperiment(
                id=report_id if k == 1 else f"{report_id}#{k}",
                question=question
                if k == 1
                else question + _table_suffix(start, lines, table_lines),
                source=source,
                task_set_description=task_set_description,
                task_set_hash=task_set_hash,
                baseline=baseline,
                arms=arms,
                paired=(),
                verdict=verdict,
                date=date,
                date_source=date_source,
            )
        )
        table_ends.append(end)
        paired.append([])

    for i, line in enumerate(lines):
        if not line.lstrip().startswith(("-", "*")):
            continue
        test, problem = _paired_line(line, _intro_line(lines, i), i + 1)
        if problem is not None:
            problems.append(problem)
            continue
        if test is None:
            continue
        if not experiments:
            problems.append(
                f"line {i + 1}: a paired test appears before any results table; "
                "it is dropped"
            )
            continue
        target = 0
        for pos, table_end in enumerate(table_ends):
            if table_end <= i:
                target = pos
        paired[target].append(test)

    out: list[ModelImportedExperiment] = []
    for exp, tests in zip(experiments, paired, strict=True):
        if tests:
            exp = exp.model_copy(update={"paired": tuple(tests)})
        out.append(exp)
    return tuple(out), tuple(problems)


def parse_report(
    text: str,
    *,
    report_id: str,
    source: str,
    date: str,
    date_source: DateSource,
    hash_markers: Sequence[str],
) -> tuple[tuple[ModelImportedExperiment, ...], tuple[str, ...]]:
    """The report's experiments, or the problems that kept it from being read. Never raises."""
    try:
        return _parse_report(
            text,
            report_id=report_id,
            source=source,
            date=date,
            date_source=date_source,
            hash_markers=tuple(hash_markers),
        )
    except Exception as exc:
        # A malformed report is a list of problems, never an exception.
        return (), (
            f"the report {report_id!r} could not be read: {str(exc).strip()[:200]}",
        )


def split_remote_listing(
    stdout: str,
) -> tuple[tuple[ReportFile, ...], tuple[str, ...]]:
    """The files of the lab's listing, or the header lines that kept them from being read."""
    files: list[ReportFile] = []
    problems: list[str] = []
    lines = stdout.splitlines()
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        if not line.startswith(_FILE_HEADER):
            i += 1
            continue
        parts = line[len(_FILE_HEADER) :].split()
        if len(parts) != 2:
            problems.append(f"malformed file header {line.strip()!r}; it is skipped")
            i += 1
            continue
        name, mtime = parts
        try:
            mtime_epoch = int(mtime)
        except ValueError:
            problems.append(f"malformed file header {line.strip()!r}; it is skipped")
            i += 1
            continue
        end = i + 1
        while end < n and not lines[end].startswith(_FILE_HEADER):
            end += 1
        files.append(
            ReportFile(
                name=name, mtime_epoch=mtime_epoch, text="\n".join(lines[i + 1 : end])
            )
        )
        i = end
    return tuple(files), tuple(problems)
