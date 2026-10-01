# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure review citation, symbol, and recorded test checks."""

import builtins
import re
from dataclasses import dataclass, field

from omnimarket.nodes.node_delegation_rubric_check_compute.handlers.criteria_common import (
    CITED_LINES_PATTERN,
    compact,
    normalized,
    result,
    test_passes,
)
from omnimarket.nodes.node_delegation_rubric_check_compute.models import (
    EnumRubricOutcome,
    ModelCitedLinesParams,
    ModelNamedSymbolsParams,
    ModelRubricCheckRequest,
    ModelRubricCriterion,
    ModelRubricCriterionResult,
)

_BUILTINS = frozenset(dir(builtins))


@dataclass
class _DiffFile:
    """Local parsing scratch state, discarded after each invocation."""

    old_ranges: list[tuple[int, int]] = field(default_factory=list)
    new_ranges: list[tuple[int, int]] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    new_lines: list[str] = field(default_factory=list)


def _diff_files(context: str) -> dict[str, _DiffFile]:
    files: dict[str, _DiffFile] = {}
    current: _DiffFile | None = None
    in_hunk = False
    old_remaining = new_remaining = 0
    for line in context.splitlines():
        if current is not None and in_hunk:
            if line.startswith("\\ No newline"):
                continue
            if line.startswith(("+", "-", " ")):
                old_step = int(not line.startswith("+"))
                new_step = int(not line.startswith("-"))
                if old_remaining >= old_step and new_remaining >= new_step:
                    current.lines.append(line[1:])
                    if line.startswith("-"):
                        current.removed.append(line[1:])
                    else:
                        current.new_lines.append(line[1:])
                    old_remaining -= old_step
                    new_remaining -= new_step
                    in_hunk = bool(old_remaining or new_remaining)
                    continue
            in_hunk = False
        header = re.match(r"diff --git a/(.+?) b/(.+)$", line)
        if header:
            current = _DiffFile()
            files[header[1]] = current
            files[header[2]] = current
        elif line.startswith("--- "):
            path = line.removeprefix("--- ").split("\t", 1)[0].removeprefix("a/")
            if path != "/dev/null":
                if current is None or current.new_ranges or current.old_ranges:
                    current = _DiffFile()
                files[path] = current
        elif line.startswith("+++ "):
            path = line.removeprefix("+++ ").split("\t", 1)[0].removeprefix("b/")
            if path != "/dev/null":
                if current is None:
                    current = _DiffFile()
                files[path] = current
        elif current is not None:
            hunk = re.match(
                r"@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? \+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@",
                line,
            )
            if hunk:
                old_start, new_start = int(hunk["old_start"]), int(hunk["new_start"])
                old_count = (
                    int(hunk["old_count"]) if hunk["old_count"] is not None else 1
                )
                new_count = (
                    int(hunk["new_count"]) if hunk["new_count"] is not None else 1
                )
                if old_count:
                    current.old_ranges.append((old_start, old_start + old_count - 1))
                if new_count:
                    current.new_ranges.append((new_start, new_start + new_count - 1))
                old_remaining, new_remaining = old_count, new_count
                in_hunk = bool(old_count or new_count)
    return files


def cited_lines_exist(
    request: ModelRubricCheckRequest, criterion: ModelRubricCriterion
) -> ModelRubricCriterionResult:
    params = criterion.params
    assert isinstance(params, ModelCitedLinesParams)
    citations = tuple(
        re.finditer(
            CITED_LINES_PATTERN,
            request.answer_text,
        )
    )
    if not citations:
        answer = request.answer_text.strip()
        if answer in params.no_findings_sentinels and answer in request.request_text:
            return result(
                criterion,
                EnumRubricOutcome.PASS,
                "no_findings_declared",
                facts=(answer,),
            )
        return result(criterion, EnumRubricOutcome.UNDETERMINED, "no_citations")
    files = _diff_files(request.context)
    if not files:
        return result(criterion, EnumRubricOutcome.UNDETERMINED, "no_line_map")
    checked: list[str] = []
    unmapped: list[str] = []
    for citation in citations:
        path = citation["path"]
        fact = citation[0]
        if path not in files:
            return result(
                criterion, EnumRubricOutcome.FAIL, "path_not_in_context", fact, (fact,)
            )
        file = files[path]
        line_start = request.answer_text.rfind("\n", 0, citation.start()) + 1
        line_end = request.answer_text.find("\n", citation.end())
        finding = (
            request.answer_text[line_start:]
            if line_end < 0
            else request.answer_text[line_start:line_end]
        )
        quotes = tuple(
            compact(quote)
            for quote in re.findall(r"`([^`\n]+)`", finding)
            if len(normalized(quote)) >= params.min_quote_chars
            and not re.fullmatch(r"[\w./-]+:\d+(?:-\d+)?", quote)
        )
        hunk_text = compact("\n".join(file.lines))
        for quote in quotes:
            if quote not in hunk_text:
                return result(
                    criterion,
                    EnumRubricOutcome.FAIL,
                    "quote_not_found",
                    quote,
                    (fact, quote),
                )
        removed_text = compact("\n".join(file.removed))
        new_text = compact("\n".join(file.new_lines))
        removed_only = any(
            quote in removed_text and quote not in new_text for quote in quotes
        )
        shared = any(quote in removed_text and quote in new_text for quote in quotes)
        ranges = file.old_ranges if removed_only else file.new_ranges
        if shared and not removed_only:
            ranges = file.new_ranges + file.old_ranges
        start = int(citation["start"])
        end = int(citation["end"]) if citation["end"] is not None else start
        if not file.old_ranges and not file.new_ranges:
            unmapped.append(fact)
            continue
        if end < start or not any(
            low - params.line_tolerance <= start and end <= high + params.line_tolerance
            for low, high in ranges
        ):
            return result(
                criterion, EnumRubricOutcome.FAIL, "line_outside_hunks", fact, (fact,)
            )
        checked.append(fact)
    if unmapped:
        return result(
            criterion,
            EnumRubricOutcome.UNDETERMINED,
            "no_line_map",
            facts=tuple(unmapped),
        )
    return result(
        criterion, EnumRubricOutcome.PASS, "citations_verified", facts=tuple(checked)
    )


def named_symbols_exist(
    request: ModelRubricCheckRequest, criterion: ModelRubricCriterion
) -> ModelRubricCriterionResult:
    params = criterion.params
    assert isinstance(params, ModelNamedSymbolsParams)
    candidates: list[str] = []
    for token in re.findall(r"`([^`\n]+)`", request.answer_text):
        value = token.strip()
        identifier = re.fullmatch(
            r"([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)(?:\([^\n]*\)?)?", value
        )
        if identifier is None:
            continue
        symbol = identifier[1]
        if symbol in params.ignore_symbols or (
            params.ignore_python_builtins and symbol.split(".")[0] in _BUILTINS
        ):
            continue
        camel = re.search(r"[a-z]", symbol) is not None and (
            re.search(r"[A-Z]", symbol[1:]) is not None
        )
        if "." in symbol or "_" in symbol or camel or "(" in value:
            candidates.append(symbol)
    symbols = tuple(dict.fromkeys(candidates))
    if not symbols:
        return result(criterion, EnumRubricOutcome.UNDETERMINED, "no_candidate_symbols")
    missing = tuple(
        symbol
        for symbol in symbols
        if re.search(r"(?<!\w)" + re.escape(symbol) + r"(?!\w)", request.context)
        is None
    )
    if len(missing) / len(symbols) > params.max_missing_fraction:
        return result(
            criterion,
            EnumRubricOutcome.FAIL,
            "symbol_not_in_context",
            ", ".join(missing),
            missing,
        )
    return result(criterion, EnumRubricOutcome.PASS, "symbols_verified", facts=symbols)


def named_test_passes(
    request: ModelRubricCheckRequest, criterion: ModelRubricCriterion
) -> ModelRubricCriterionResult:
    return test_passes(request, criterion, include_request=False)
