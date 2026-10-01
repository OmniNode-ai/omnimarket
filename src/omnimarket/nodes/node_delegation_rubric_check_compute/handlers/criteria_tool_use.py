# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure checks over caller-recorded tool calls and workspace evidence."""

import json
import re

from omnimarket.nodes.node_delegation_rubric_check_compute.handlers.criteria_common import (
    CITED_LINES_PATTERN,
    compact,
    normalized,
    result,
    test_passes,
)
from omnimarket.nodes.node_delegation_rubric_check_compute.handlers.criteria_summarization import (
    _missing,
    answer_units,
)
from omnimarket.nodes.node_delegation_rubric_check_compute.models import (
    EnumRubricOutcome,
    EnumToolCallStatus,
    EnumToolParameterType,
    ModelEditsApplyParams,
    ModelNoPhantomPathsParams,
    ModelRubricCheckRequest,
    ModelRubricCriterion,
    ModelRubricCriterionResult,
    ModelTaskAnswerTraceableParams,
    ModelToolCallsWellformedParams,
    ModelWithinBudgetParams,
    ModelWorkspaceFile,
)

# A file citation with one or more lines: ``path.py:24``, ``path.py:13-17``,
# ``path.py:111,264``. The path part matches the file-name extraction of
# claims_traceable, so a traced citation leaves no token or number behind.
_LINE_CITATION = re.compile(
    r"(?<![\w/.-])(?P<path>(?:[\w.-]+/)*[\w.-]+\.[A-Za-z][\w-]*)"
    r":(?P<lines>\d+(?:\s*[-,]\s*\d+)*)(?![\w.])"
)
# A ``git status --short`` row: one or two status letters, then the path.
_STATUS_ROW = re.compile(r"[MADRCU?!]{1,2}\s+(?P<path>[\w./-]+)")
# A dotted member reference: ``DelegateResult.harness_receipt``.
_MEMBER = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+")
_CALL = re.compile(r"(?P<name>\.?[A-Za-z_][\w.]*)\((?P<args>.*)\)", re.DOTALL)
_COMPARISON = re.compile(r"(?P<left>.+?)\s*(?:==|!=)\s*(?P<right>.+)", re.DOTALL)


def _arguments(text: str) -> dict[str, object] | None:
    try:
        value: object = json.loads(text)
    except (ValueError, RecursionError):
        return None
    if not isinstance(value, dict):
        return None
    return {str(key): item for key, item in value.items()}


def _matches_type(value: object, expected: EnumToolParameterType) -> bool:
    if expected == EnumToolParameterType.INTEGER:
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == EnumToolParameterType.NUMBER:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == EnumToolParameterType.NULL:
        return value is None
    types = {
        EnumToolParameterType.STRING: str,
        EnumToolParameterType.BOOLEAN: bool,
        EnumToolParameterType.ARRAY: list,
        EnumToolParameterType.OBJECT: dict,
    }
    return isinstance(value, types[expected])


def _argument_strings(text: str) -> tuple[str, ...]:
    """The decoded string values of a call's arguments, at any depth.

    The raw JSON escapes quotes and newlines, so text a model wrote through an
    accepted edit (``extra="forbid"``) never matches its own answer verbatim.
    """
    found: list[str] = []
    pending: list[object] = [_arguments(text)]
    while pending:
        value = pending.pop()
        if isinstance(value, str):
            found.append(value)
        elif isinstance(value, dict):
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
    return tuple(found)


def _path_exists(path: str, files: set[str]) -> bool:
    """A file of the tree, or a directory holding one (a search tool's ``path``)."""
    if path in files or path in {"", "."}:
        return True
    directory = path.rstrip("/") + "/"
    return any(file.startswith(directory) for file in files)


def tool_calls_wellformed(
    request: ModelRubricCheckRequest, criterion: ModelRubricCriterion
) -> ModelRubricCriterionResult:
    transcript = request.transcript
    if transcript is None:
        return result(criterion, EnumRubricOutcome.UNDETERMINED, "no_transcript")
    params = criterion.params
    assert isinstance(params, ModelToolCallsWellformedParams)
    if not transcript.tool_calls:
        return result(criterion, EnumRubricOutcome.UNDETERMINED, "no_tool_calls")
    tools = {tool.name: tool for tool in transcript.declared_tools}
    for call in transcript.tool_calls:
        tool = tools.get(call.tool_name)
        if tool is None:
            return result(
                criterion,
                EnumRubricOutcome.FAIL,
                "undeclared_tool",
                call.call_id,
                (call.call_id,),
            )
        arguments = _arguments(call.arguments_json)
        if arguments is None:
            return result(
                criterion,
                EnumRubricOutcome.FAIL,
                "arguments_unparsable",
                call.call_id,
                (call.call_id,),
            )
        parameters = {param.name: param for param in tool.parameters}
        for param in tool.parameters:
            if param.required and param.name not in arguments:
                return result(
                    criterion,
                    EnumRubricOutcome.FAIL,
                    "missing_required_argument",
                    f"{call.call_id}:{param.name}",
                    (call.call_id, param.name),
                )
        if not params.allow_extra_arguments:
            for name in arguments:
                if name not in parameters:
                    return result(
                        criterion,
                        EnumRubricOutcome.FAIL,
                        "unknown_argument",
                        f"{call.call_id}:{name}",
                        (call.call_id, name),
                    )
        for name, value in arguments.items():
            declared = parameters.get(name)
            if declared is not None and not _matches_type(value, declared.json_type):
                return result(
                    criterion,
                    EnumRubricOutcome.FAIL,
                    "argument_type_mismatch",
                    f"{call.call_id}:{name}",
                    (call.call_id, name),
                )
    return result(
        criterion,
        EnumRubricOutcome.PASS,
        "calls_wellformed",
        facts=tuple(call.call_id for call in transcript.tool_calls),
    )


def no_phantom_paths(
    request: ModelRubricCheckRequest, criterion: ModelRubricCriterion
) -> ModelRubricCriterionResult:
    transcript = request.transcript
    if transcript is None:
        return result(criterion, EnumRubricOutcome.UNDETERMINED, "no_transcript")
    params = criterion.params
    assert isinstance(params, ModelNoPhantomPathsParams)
    if transcript.workspace_files is None:
        return result(
            criterion, EnumRubricOutcome.UNDETERMINED, "no_workspace_manifest"
        )
    known = {
        file.path.removeprefix("./"): file.line_count
        for file in transcript.workspace_files
    }
    created: set[str] = set()
    checked: list[str] = []
    unmapped: list[str] = []
    for call in transcript.tool_calls:
        arguments = _arguments(call.arguments_json)
        if arguments is None:
            continue
        for name in params.path_argument_names:
            value = arguments.get(name)
            if not isinstance(value, str):
                continue
            path = value.removeprefix("./")
            fact = f"{call.call_id}:{path}"
            if (
                call.tool_name in params.creating_tools
                and call.result is not None
                and call.result.status == EnumToolCallStatus.OK
            ):
                created.add(path)
            elif not _path_exists(path, set(known) | created):
                return result(
                    criterion, EnumRubricOutcome.FAIL, "phantom_path", fact, (fact,)
                )
            checked.append(fact)
    for citation in re.finditer(CITED_LINES_PATTERN, request.answer_text):
        path = citation["path"].removeprefix("./")
        fact = citation[0]
        if path not in known and path not in created:
            return result(
                criterion, EnumRubricOutcome.FAIL, "phantom_path", fact, (fact,)
            )
        if path not in known:
            unmapped.append(fact)
            continue
        end = int(citation["end"] or citation["start"])
        if end > known[path] + params.line_tolerance:
            return result(
                criterion, EnumRubricOutcome.FAIL, "line_past_end", fact, (fact,)
            )
        checked.append(fact)
    if unmapped:
        return result(
            criterion,
            EnumRubricOutcome.UNDETERMINED,
            "no_line_map",
            facts=tuple(unmapped),
        )
    if not checked:
        return result(criterion, EnumRubricOutcome.UNDETERMINED, "not_applicable")
    return result(
        criterion, EnumRubricOutcome.PASS, "paths_verified", facts=tuple(checked)
    )


def edits_apply(
    request: ModelRubricCheckRequest, criterion: ModelRubricCriterion
) -> ModelRubricCriterionResult:
    transcript = request.transcript
    if transcript is None:
        return result(criterion, EnumRubricOutcome.UNDETERMINED, "no_transcript")
    params = criterion.params
    assert isinstance(params, ModelEditsApplyParams)
    calls = tuple(
        call for call in transcript.tool_calls if call.tool_name in params.edit_tools
    )
    if not calls:
        return result(criterion, EnumRubricOutcome.UNDETERMINED, "not_applicable")
    for call in calls:
        if call.result is not None and call.result.status == EnumToolCallStatus.ERROR:
            return result(
                criterion,
                EnumRubricOutcome.FAIL,
                "edit_failed",
                call.call_id,
                (call.call_id,),
            )
    if any(call.result is None for call in calls):
        return result(criterion, EnumRubricOutcome.UNDETERMINED, "no_edit_result")
    return result(
        criterion,
        EnumRubricOutcome.PASS,
        "edits_applied",
        facts=tuple(call.call_id for call in calls),
    )


def stated_check_passes(
    request: ModelRubricCheckRequest, criterion: ModelRubricCriterion
) -> ModelRubricCriterionResult:
    if request.transcript is None:
        return result(criterion, EnumRubricOutcome.UNDETERMINED, "no_transcript")
    return test_passes(request, criterion, include_request=False)


class _Evidence:
    """What a run observed, in the forms the traceability shapes compare against."""

    def __init__(
        self,
        chunks: tuple[str, ...],
        commands: tuple[str, ...],
        manifest: tuple[ModelWorkspaceFile, ...] | None,
        params: ModelTaskAnswerTraceableParams,
    ) -> None:
        source = "\n".join(chunks)
        self.source = source
        self.chunks = tuple(compact(chunk) for chunk in chunks)
        self.normalized = normalized(source)
        self.folded = self.normalized.casefold()
        self.compact = compact(source)
        self.commands = tuple(
            tuple(word.strip("\"'") for word in command.split()) for command in commands
        )
        self.lines = (
            None
            if manifest is None
            else {row.path.removeprefix("./"): row.line_count for row in manifest}
        )
        self.params = params

    def has(self, text: str) -> bool:
        """Whitespace-insensitive: a call the answer reflows still matches."""
        return bool(text) and compact(text) in self.compact

    def together(self, parts: list[str]) -> bool:
        """Every part inside one recorded output or argument, not merely somewhere in the run."""
        wanted = [compact(part) for part in parts]
        return any(all(part in chunk for part in wanted) for chunk in self.chunks)

    def status_row(self, path: str) -> bool:
        """A ``git status --short`` row for this path that the run actually printed."""
        return bool(
            re.search(
                r"(?m)^[ \t]*[MADRCU?!]{1,2}[ \t]+" + re.escape(path) + r"[ \t]*$",
                self.source,
            )
        )

    def citation_fits(self, path: str, lines: str) -> bool:
        """A cited file the run saw, and (where a manifest maps it) lines inside it."""
        if not self.has(path):
            return False
        if self.lines is None:
            return True
        clean = path.removeprefix("./")
        known = [
            count
            for name, count in self.lines.items()
            if name == clean or name.endswith("/" + clean)
        ]
        if len(known) != 1:
            return True
        cited = (int(value) for value in re.findall(r"\d+", lines))
        return all(
            value <= known[0] + self.params.citation_line_tolerance for value in cited
        )

    def ran(self, words: list[str]) -> bool:
        """The words, in order, of one command the run issued."""
        for command in self.commands:
            position = 0
            for word in command:
                if position < len(words) and word == words[position]:
                    position += 1
            if words and position == len(words):
                return True
        return False


def _split_arguments(text: str) -> list[str]:
    """Top-level comma split of a call's argument text."""
    parts: list[str] = []
    depth = 0
    quote = ""
    current: list[str] = []
    for char in text:
        if quote:
            quote = "" if char == quote else quote
        elif char in "\"'":
            quote = char
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        elif char == "," and depth == 0:
            parts.append("".join(current))
            current = []
            continue
        current.append(char)
    parts.append("".join(current))
    return parts


def _code_traced(text: str, evidence: _Evidence) -> bool:
    """A code span the run saw verbatim, or built only from parts it saw."""
    text = text.strip()
    markers = evidence.params.elision_markers
    if not text or text in markers or evidence.has(text):
        return True
    call = _CALL.fullmatch(text)
    if call:
        return evidence.has(call["name"].lstrip(".")) and all(
            _code_traced(part, evidence) for part in _split_arguments(call["args"])
        )
    if any(marker in text for marker in markers):
        fragments = re.split("|".join(re.escape(marker) for marker in markers), text)
        return all(_code_traced(part, evidence) for part in fragments)
    if _MEMBER.fullmatch(text):
        # A qualified name the run never printed whole: every part, seen together.
        parts = text.split(".")
        return all(evidence.has(part) for part in parts) and evidence.together(parts)
    comparison = _COMPARISON.fullmatch(text)
    if comparison:
        return _code_traced(comparison["left"], evidence) and _code_traced(
            comparison["right"], evidence
        )
    return False


def _shape_traced(token: str, unit: str, evidence: _Evidence) -> bool:
    """A missing token whose shape, not its fact, kept it from matching."""
    params = evidence.params
    if token in params.ignore_tokens:
        return True
    stripped = token.rstrip(params.trailing_punctuation).strip()
    if f'"{token}"' in unit:
        # A quotation: the quoted words, in any case, without the sentence's
        # closing mark. Every word still has to be there, in order.
        return bool(stripped) and normalized(stripped).casefold() in evidence.folded
    if stripped != token and evidence.has(stripped):
        return True
    status = _STATUS_ROW.fullmatch(stripped)
    if status:
        # A status row is a claim about what git printed: the path alone is not enough.
        return evidence.status_row(status["path"])
    words = [
        word.strip("\"'")
        for word in stripped.split()
        if word not in params.elision_markers
    ]
    if len(words) > 1 and evidence.ran(words):
        return True
    # A slash-joined keyword pair (``try/except X``) names a construct, not a fact.
    keywords = [
        word
        for word in stripped.split()
        if "/" in word and all(part in params.code_keywords for part in word.split("/"))
    ]
    if keywords:
        rest = " ".join(word for word in stripped.split() if word not in keywords)
        return not rest or _code_traced(rest, evidence)
    return _code_traced(stripped, evidence)


def _resolve_citations(unit: str, evidence: _Evidence) -> str:
    """A traced ``path:line`` citation reads as its path, so its line numbers are not claims."""
    return _LINE_CITATION.sub(
        lambda match: (
            match["path"]
            if evidence.citation_fits(match["path"], match["lines"])
            else match[0]
        ),
        unit,
    )


def task_answer_traceable(
    request: ModelRubricCheckRequest, criterion: ModelRubricCriterion
) -> ModelRubricCriterionResult:
    transcript = request.transcript
    if transcript is None:
        return result(criterion, EnumRubricOutcome.UNDETERMINED, "no_transcript")
    params = criterion.params
    assert isinstance(params, ModelTaskAnswerTraceableParams)
    # What the run observed: every recorded output, plus the arguments of calls
    # the environment accepted (a path an ok Read was given exists). A failed
    # call's arguments are the model's own text and are not evidence, except
    # that the command it issued is a fact about the run ("pytest was refused").
    chunks = (
        request.request_text,
        *(
            call.result.output
            for call in transcript.tool_calls
            if call.result is not None
        ),
        *(
            text
            for call in transcript.tool_calls
            if call.result is not None and call.result.status == EnumToolCallStatus.OK
            for text in (
                call.arguments_json,
                *_argument_strings(call.arguments_json),
            )
        ),
    )
    commands = tuple(
        value
        for call in transcript.tool_calls
        if call.result is not None
        for name in params.command_argument_names
        if isinstance(value := (_arguments(call.arguments_json) or {}).get(name), str)
    )
    evidence = _Evidence(chunks, commands, transcript.workspace_files, params)
    source = evidence.source
    units = tuple(
        _resolve_citations(unit, evidence) for unit in answer_units(request.answer_text)
    )
    missing = tuple(
        dict.fromkeys(
            token
            for unit in units
            for token in _missing(unit, source, params)
            if not _shape_traced(token, unit, evidence)
        )
    )
    if missing:
        facts = missing[: params.max_facts]
        return result(
            criterion,
            EnumRubricOutcome.FAIL,
            "untraceable_identifier",
            ", ".join(facts),
            facts,
        )
    identifiers = tuple(
        dict.fromkeys(
            token
            for unit in answer_units(request.answer_text)
            for token in _missing(unit, "", params)
        )
    )
    if not identifiers:
        return result(criterion, EnumRubricOutcome.UNDETERMINED, "no_identifiers")
    return result(
        criterion,
        EnumRubricOutcome.PASS,
        "answer_traceable",
        facts=identifiers[: params.max_facts],
    )


def within_budget(
    request: ModelRubricCheckRequest, criterion: ModelRubricCriterion
) -> ModelRubricCriterionResult:
    transcript = request.transcript
    if transcript is None:
        return result(criterion, EnumRubricOutcome.UNDETERMINED, "no_transcript")
    params = criterion.params
    assert isinstance(params, ModelWithinBudgetParams)
    wall_limit = params.wall_time_limit_ms(transcript.engine)
    facts = (
        f"turns={transcript.turn_count}",
        f"tool_calls={len(transcript.tool_calls)}",
        f"wall_time_ms={transcript.wall_time_ms if transcript.wall_time_ms is not None else 'absent'}",
        f"engine={transcript.engine or 'absent'}",
        f"wall_time_limit_ms={wall_limit}",
    )
    exceeded: list[str] = []
    for name, value, limit in (
        ("turns", transcript.turn_count, params.max_turns),
        ("tool_calls", len(transcript.tool_calls), params.max_tool_calls),
        ("wall_time_ms", transcript.wall_time_ms, wall_limit),
    ):
        if value is not None and value > limit:
            exceeded.append(f"{name}={value} > {limit}")
    if exceeded:
        return result(
            criterion,
            EnumRubricOutcome.FAIL,
            "budget_exceeded",
            ", ".join(exceeded),
            facts,
        )
    if transcript.wall_time_ms is None:
        return result(
            criterion, EnumRubricOutcome.UNDETERMINED, "no_wall_time", facts=facts
        )
    return result(criterion, EnumRubricOutcome.PASS, "within_budget", facts=facts)
