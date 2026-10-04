# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""State the facts a script can compute from a task before the task itself.

OMN-19432. On 2026-09-30 the 60 first-run false passes of the delegation gate
(answers the gate accepted and two blind raters called inadequate) were
re-asked under controlled changes. Asking again with the prompt unchanged
recovered 16 on average; asking facts-first recovered 33 (a second draw, 28),
at no model cost, and a stronger model with the original prompt recovered the
same 33. Data: knowledge-base-internal
``reports/delegation-evals/2026-09-30-wrong-answer-why``.

What the measured prompt stated, and what this module computes the same way:

* the task's own constraints as a numbered list, each one a sentence copied
  from the task, never a paraphrase;
* counts and limits computed in code: the lines, words and sentences of the
  task, and any numeric limit it states ("at most 100 words");
* every identifier the input contains, so an identifier outside the list is
  invented;
* the lines of any code or diff in the input, numbered (a diff by its hunk
  headers), so a cited line number can be checked against the input;
* a caller-cut input marked as cut (a hunk whose header declares more lines
  than the input holds, a code fence never closed, a literal cut marker).

It adds nothing the task did not state: every sentence, identifier and line it
repeats is the task's own, and every count is exact. The task itself follows
unchanged, so nothing the caller wrote is lost or reworded.

Which task classes send this shape, and every cue word and bound used here, are
declared in ``task_class_contracts.v1.yaml`` (``prompt_shape`` on a class and
the ``facts_first_prompt`` section). The caller resolves them and hands the
policy in; this handler names no task class and reads no file.

The handler is pure: the same request gives the same bytes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from omnimarket.inference.task_class_authority import ModelFactsFirstPromptPolicy
from omnimarket.nodes.node_facts_first_prompt_compute.models.model_facts_first_prompt_request import (
    ModelFactsFirstPromptRequest,
)
from omnimarket.nodes.node_facts_first_prompt_compute.models.model_facts_first_prompt_result import (
    ModelFactsFirstPromptResult,
)

__all__ = [
    "FACTS_FIRST_HEADER",
    "TASK_HEADER",
    "HandlerFactsFirstPrompt",
]

FACTS_FIRST_HEADER = "Facts computed from the task text by code (exact, not a reading):"
TASK_HEADER = "The task, exactly as given:"

_FENCE_OPEN = re.compile(r"^\s*(?:```|~~~)\s*([\w+.#-]*)\s*$")
_FENCE_CLOSE = re.compile(r"^\s*(?:```|~~~)\s*$")
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
_BULLET_PREFIX = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+")
_NUMBER_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
}

_IDENTIFIER_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Order is priority: an earlier pattern claims its span before a later one
    # can list a piece of it.
    re.compile(r"`([^`\s]{1,80})`"),
    re.compile(
        r"(?<![\w/.-])(?:[\w.-]+/)+[\w.-]+\.\w+\b"
        r"|\b[\w-]+\.(?:py|ts|tsx|js|yaml|yml|json|md|sh|toml|rs|go|sql)\b"
    ),
    re.compile(r"\bOMN-\d+\b"),
    re.compile(r"\b[\w.-]+#\d+\b"),
    re.compile(r"\b(?!(?:e\.g|i\.e)\b)[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+\b"),
    re.compile(r"\b(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}\b"),
    re.compile(r"\b(?:def|class|function|const|let|var|fn|func)\s+([A-Za-z_]\w*)"),
    re.compile(r"\b([A-Za-z_]\w*)\("),
    re.compile(r"\b[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]*)+\b|\b[a-z]+(?:[A-Z][a-z0-9]*)+\b"),
    re.compile(r"\b_{0,2}[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+_{0,2}\b"),
)


@dataclass
class _Hunk:
    header: str
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    rows: list[tuple[str, int, str]] = field(default_factory=list)
    old_seen: int = 0
    new_seen: int = 0

    @property
    def complete(self) -> bool:
        return self.old_seen == self.old_count and self.new_seen == self.new_count


@dataclass
class _Block:
    language: str
    lines: list[str]
    closed: bool


@dataclass
class _Scan:
    prose: list[str] = field(default_factory=list)
    blocks: list[_Block] = field(default_factory=list)
    hunks: list[_Hunk] = field(default_factory=list)
    unclosed_fence_line: int | None = None


def _consume_hunk(lines: list[str], start: int) -> tuple[_Hunk, int]:
    match = _HUNK.match(lines[start])
    assert match is not None
    old_start, old_count, new_start, new_count = match.groups()
    hunk = _Hunk(
        header=match.group(0),
        old_start=int(old_start),
        old_count=int(old_count) if old_count is not None else 1,
        new_start=int(new_start),
        new_count=int(new_count) if new_count is not None else 1,
    )
    old_no, new_no = hunk.old_start, hunk.new_start
    i = start + 1
    while i < len(lines) and not hunk.complete:
        line = lines[i]
        if line.startswith(("@@", "diff ")):
            break
        sign = line[:1]
        if sign == "\\":
            i += 1
            continue
        if sign == "-":
            if hunk.old_seen >= hunk.old_count:
                break
            hunk.rows.append(("old", old_no, line))
            old_no += 1
            hunk.old_seen += 1
        elif sign == "+":
            if hunk.new_seen >= hunk.new_count:
                break
            hunk.rows.append(("new", new_no, line))
            new_no += 1
            hunk.new_seen += 1
        elif sign in {" ", ""}:
            if hunk.old_seen >= hunk.old_count or hunk.new_seen >= hunk.new_count:
                break
            hunk.rows.append(("new", new_no, line))
            old_no += 1
            new_no += 1
            hunk.old_seen += 1
            hunk.new_seen += 1
        else:
            break
        i += 1
    return hunk, i


def _hunks_in(lines: list[str]) -> list[_Hunk]:
    hunks: list[_Hunk] = []
    i = 0
    while i < len(lines):
        if _HUNK.match(lines[i]):
            hunk, i = _consume_hunk(lines, i)
            hunks.append(hunk)
        else:
            i += 1
    return hunks


def _scan(text: str) -> _Scan:
    lines = text.split("\n")
    scan = _Scan()
    i = 0
    while i < len(lines):
        opener = _FENCE_OPEN.match(lines[i])
        if opener is not None:
            body: list[str] = []
            j = i + 1
            while j < len(lines) and not _FENCE_CLOSE.match(lines[j]):
                body.append(lines[j])
                j += 1
            closed = j < len(lines)
            if not closed and scan.unclosed_fence_line is None:
                scan.unclosed_fence_line = i + 1
            hunks = _hunks_in(body)
            if hunks:
                scan.hunks.extend(hunks)
            else:
                scan.blocks.append(_Block(opener.group(1), body, closed))
            i = j + 1 if closed else j
            continue
        if _HUNK.match(lines[i]):
            hunk, i = _consume_hunk(lines, i)
            scan.hunks.append(hunk)
            continue
        scan.prose.append(lines[i])
        i += 1
    return scan


def _phrase_alternation(phrases: tuple[str, ...]) -> str:
    ordered = sorted(set(phrases), key=len, reverse=True)
    return "|".join(re.escape(phrase) for phrase in ordered)


def _constraints(prose: list[str], policy: ModelFactsFirstPromptPolicy) -> list[str]:
    cue = re.compile(
        rf"(?<![\w'])(?:{_phrase_alternation(policy.constraint_cues)})(?![\w'])",
        re.IGNORECASE,
    )
    found: list[str] = []
    for line in prose:
        stripped = _BULLET_PREFIX.sub("", line).strip()
        if not stripped or stripped.startswith(FACTS_FIRST_HEADER):
            continue
        for sentence in _SENTENCE_END.split(stripped):
            sentence = sentence.strip()
            if (
                sentence
                and len(sentence) <= policy.max_constraint_chars
                and cue.search(sentence)
                and sentence not in found
            ):
                found.append(sentence)
    return found


def _limits(prose: list[str], policy: ModelFactsFirstPromptPolicy) -> list[str]:
    number_words = "|".join(_NUMBER_WORDS)
    pattern = re.compile(
        rf"\b({_phrase_alternation(policy.limit_cues)})\s+(\d+|{number_words})\s+"
        rf"({_phrase_alternation(policy.limit_units)})\b",
        re.IGNORECASE,
    )
    found: list[str] = []
    for match in pattern.finditer("\n".join(prose)):
        count = match.group(2).lower()
        rendered = (
            f"{match.group(1).lower()} {_NUMBER_WORDS.get(count, count)} "
            f"{match.group(3).lower()}"
        )
        if rendered not in found:
            found.append(rendered)
    return found


def _identifiers(text: str) -> list[str]:
    claimed: list[tuple[int, int]] = []
    hits: list[tuple[int, str]] = []
    for pattern in _IDENTIFIER_PATTERNS:
        for match in pattern.finditer(text):
            span = match.span()
            if any(span[0] < end and start < span[1] for start, end in claimed):
                continue
            claimed.append(span)
            token = match.group(1) if match.groups() else match.group(0)
            hits.append((span[0], token))
    ordered: list[str] = []
    for _, token in sorted(hits):
        if token not in ordered:
            ordered.append(token)
    return ordered


def _sentence_count(prose: list[str]) -> int:
    count = 0
    for line in prose:
        stripped = _BULLET_PREFIX.sub("", line).strip()
        if stripped:
            count += len([s for s in _SENTENCE_END.split(stripped) if s.strip()])
    return count


def _cut_facts(
    scan: _Scan, text: str, policy: ModelFactsFirstPromptPolicy
) -> list[str]:
    facts: list[str] = []
    for hunk in scan.hunks:
        if not hunk.complete:
            facts.append(
                f"hunk {hunk.header} declares {hunk.old_count} old and "
                f"{hunk.new_count} new lines but the input holds {hunk.old_seen} "
                f"and {hunk.new_seen}"
            )
    if scan.unclosed_fence_line is not None:
        facts.append(
            f"the code fence opened at task line {scan.unclosed_fence_line} is "
            "never closed"
        )
    lowered = text.lower()
    facts.extend(
        f"the input carries the cut marker {marker!r}"
        for marker in policy.truncation_markers
        if marker.lower() in lowered
    )
    return facts


def _numbered_section(scan: _Scan) -> str | None:
    parts: list[str] = []
    for index, block in enumerate(scan.blocks, start=1):
        language = f" ({block.language})" if block.language else ""
        parts.append(f"Code block {index}{language}, {len(block.lines)} lines:")
        parts.extend(f"{n}: {line}" for n, line in enumerate(block.lines, start=1))
    for index, hunk in enumerate(scan.hunks, start=1):
        parts.append(
            f"Diff hunk {index}, {hunk.header} (new-side numbers for context and "
            "added lines, old-side numbers marked old for removed lines):"
        )
        parts.extend(f"{side} {number}: {line}" for side, number, line in hunk.rows)
    if not parts:
        return None
    return (
        "Numbered code lines of the input (cite these numbers; a number not "
        "listed here is not a line of the input):\n" + "\n".join(parts)
    )


def _state_facts_first(prompt: str, policy: ModelFactsFirstPromptPolicy) -> str:
    """Return ``prompt`` stated facts-first, or ``prompt`` itself with nothing to state.

    The task follows the facts unchanged. A prompt that already opens with the
    facts header is returned as it is, so applying this twice is applying it
    once.
    """
    if prompt.startswith(FACTS_FIRST_HEADER):
        return prompt
    scan = _scan(prompt)
    constraints = _constraints(scan.prose, policy)[: policy.max_constraints]
    limits = _limits(scan.prose, policy)
    identifiers = _identifiers(prompt)
    cut = _cut_facts(scan, prompt, policy)
    code_lines = sum(len(b.lines) for b in scan.blocks) + sum(
        len(h.rows) for h in scan.hunks
    )
    has_code = bool(scan.blocks or scan.hunks)
    if not (constraints or limits or identifiers or has_code or cut):
        return prompt

    lines = prompt.count("\n") + 1
    words = len(prompt.split())
    facts = [
        f"- Task text: {lines} lines, {words} words; its prose holds "
        f"{_sentence_count(scan.prose)} sentences."
    ]
    if limits:
        facts.append(f"- Limits the task states: {'; '.join(limits)}.")
    if has_code:
        facts.append(
            f"- Code in the input: {len(scan.blocks)} code blocks, "
            f"{len(scan.hunks)} diff hunks, {code_lines} lines."
        )
    if cut:
        facts.append(
            f"- The input is cut: {'; '.join(cut)}. Treat what lies past a cut as "
            "unseen: say what you could not see and make no claim about it."
        )
    sections = [FACTS_FIRST_HEADER, "\n".join(facts)]
    if identifiers:
        listed = identifiers[: policy.max_identifiers]
        if len(identifiers) <= policy.max_identifiers:
            sections.append(
                f"Identifiers that appear in the input ({len(identifiers)}, in order "
                f"of first appearance): {', '.join(listed)}. An identifier not in "
                "this list does not appear in the input: do not cite or invent one."
            )
        else:
            sections.append(
                f"Identifiers that appear in the input: the first {len(listed)} of "
                f"{len(identifiers)}, in order of first appearance: "
                f"{', '.join(listed)}."
            )
    if constraints:
        numbered = "\n".join(f"{n}. {c}" for n, c in enumerate(constraints, start=1))
        sections.append(
            "Constraints the task states, copied from it in order. Satisfy every "
            f"one:\n{numbered}"
        )
    if has_code and code_lines <= policy.max_numbered_lines:
        numbered_lines = _numbered_section(scan)
        if numbered_lines is not None:
            sections.append(numbered_lines)
    elif has_code:
        sections.append(
            f"Line numbers are not stated: the input holds {code_lines} code lines "
            f"and the limit is {policy.max_numbered_lines}."
        )
    return "\n\n".join(sections) + f"\n\n{TASK_HEADER}\n{prompt}"


class HandlerFactsFirstPrompt:
    """State a prompt's computed facts ahead of it (OMN-19432)."""

    def handle(
        self, request: ModelFactsFirstPromptRequest
    ) -> ModelFactsFirstPromptResult:
        stated = _state_facts_first(request.prompt, request.policy)
        return ModelFactsFirstPromptResult(
            prompt=stated, facts_stated=stated != request.prompt
        )
