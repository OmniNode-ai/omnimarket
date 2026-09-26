# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2026 OmniNode Team
"""Contract-declared number grounding and name resolution for the quality gate.

OMN-19529. Two checks that compare a response with the input it was derived
from, the way OMN-18297's identifier grounding does for citations:

* ``numbers_grounded`` (prose classes): every number the answer states, in
  digits or spelled out, must occur in the grounding source. The delegation
  capability matrix of 2026-09-25 recorded the gate accepting "Nine classes"
  over a table of eight and an added derived count, each at 1.0.
* ``names_resolve`` (code classes): every name the answer's Python reads must be
  bound by that code, be a builtin, or occur in the grounding source. It is the
  static half of "imports in isolation": a pure reducer must never execute
  model-written code, so a NameError is found by reading, not by running.

Both read their declaration from the gate node's ``contract.yaml`` and
hardcode no pattern or word. Both are UNEVALUATED without a grounding source;
number grounding is also UNEVALUATED when its source states no number. The
caller records them as skipped, never as passed.
"""

from __future__ import annotations

import builtins
import re
import symtable
from collections.abc import Iterable
from functools import lru_cache
from pathlib import Path

import yaml
from omnibase_infra.errors import ProtocolConfigurationError

from omnimarket.delegation.identifier_grounding import (
    answer_segment,
    resolve_identifier_grounding_policy,
)
from omnimarket.inference.delegation_config_provenance import resolve_path_config
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_numeric_grounding import (
    ModelNameResolutionPolicy,
    ModelNameResolutionVerdict,
    ModelNumericGroundingPolicy,
    ModelNumericGroundingVerdict,
    ModelUngroundedNumber,
)

_CONTRACT_PATH_CONFIG_KEY = "QUALITY_GATE_CONTRACT_PATH"
# Same file identifier grounding reads, resolved the same way and for the same
# reason: reading a file is I/O, which the node-purity gate keeps out of nodes.
_DEFAULT_CONTRACT_PATH = (
    Path(__file__).parent.parent
    / "nodes"
    / "node_delegation_quality_gate_reducer"
    / "contract.yaml"
)
_NUMERIC_SECTION = "numeric_grounding"
# The named group the contract's claim_pattern must declare.
_CLAIM_GROUP = "token"
_NAME_SECTION = "name_resolution"

_FENCE_WITH_LANG_RE = re.compile(r"```([^\r\n]*)\r?\n(.*?)```", re.DOTALL)
_FENCE_RE = re.compile(r"```")
_STAR_IMPORT_RE = re.compile(r"^\s*from\s+\S+\s+import\s+\*", re.MULTILINE)
_BUILTIN_NAMES: frozenset[str] = frozenset(dir(builtins))


def _contract_section(section: str) -> dict[str, object]:
    contract_path, _ = resolve_path_config(
        _CONTRACT_PATH_CONFIG_KEY,
        _DEFAULT_CONTRACT_PATH,
    )
    if not contract_path.exists():
        raise ProtocolConfigurationError(
            f"quality gate contract not found at {contract_path}; "
            f"{section} policy cannot be resolved"
        )
    try:
        raw = yaml.safe_load(contract_path.read_text())
    except yaml.YAMLError as exc:  # pragma: no cover - defensive parse guard
        raise ProtocolConfigurationError(
            f"quality gate contract at {contract_path} is not valid YAML: {exc}"
        ) from exc
    if not isinstance(raw, dict) or not isinstance(raw.get(section), dict):
        raise ProtocolConfigurationError(
            f"quality gate contract at {contract_path} declares no {section} section"
        )
    block: dict[str, object] = raw[section]
    return block


@lru_cache(maxsize=1)
def resolve_numeric_grounding_policy() -> ModelNumericGroundingPolicy:
    """Load the contract's ``numeric_grounding`` block. Fail-closed."""
    return ModelNumericGroundingPolicy.model_validate(
        _contract_section(_NUMERIC_SECTION)
    )


@lru_cache(maxsize=1)
def resolve_name_resolution_policy() -> ModelNameResolutionPolicy:
    """Load the contract's ``name_resolution`` block. Fail-closed."""
    return ModelNameResolutionPolicy.model_validate(_contract_section(_NAME_SECTION))


@lru_cache(maxsize=64)
def _compiled(pattern: str, flags: int = 0) -> re.Pattern[str]:
    return re.compile(pattern, flags)


# ---------------------------------------------------------------------------
# numbers_grounded
# ---------------------------------------------------------------------------


def _canonical(token: str) -> str:
    """``1,735`` -> ``1735``; ``09`` -> ``9``; ``0.50`` stays ``0.50``."""
    plain = token.replace(",", "")
    whole, dot, fraction = plain.partition(".")
    whole = whole.lstrip("0") or "0"
    return f"{whole}{dot}{fraction}"


def _spelled_number_re(words: dict[str, int], separator: str) -> re.Pattern[str]:
    """One spelled number: a tens word with an optional unit word, or any word.

    The tens alternative is tried first, so "twenty-three" is one match of 23,
    while "three four" is two matches, 3 and 4.
    """

    def _alternation(names: Iterable[str]) -> str:
        return "|".join(sorted(names, key=len, reverse=True))

    tens = [w for w, v in words.items() if v >= 20 and v % 10 == 0]
    units = [w for w, v in words.items() if 1 <= v <= 9]
    compound = (
        rf"(?P<tens>{_alternation(tens)})(?:{separator}(?P<unit>{_alternation(units)}))?"
        if tens and units
        else r"(?P<tens>(?!x)x)(?P<unit>(?!x)x)?"
    )
    return _compiled(
        rf"\b(?:{compound}|(?P<word>{_alternation(words)}))\b",
        re.IGNORECASE,
    )


def _spelled_value(match: re.Match[str], words: dict[str, int]) -> tuple[int, str]:
    """The value of one spelled-number match and the text it was read from."""
    if match.group("tens") is not None:
        value = words[match.group("tens").lower()]
        if match.group("unit") is not None:
            value += words[match.group("unit").lower()]
        return value, match.group(0)
    return words[match.group("word").lower()], match.group(0)


def _source_values(source: str, policy: ModelNumericGroundingPolicy) -> set[str]:
    values: set[str] = set()
    splitter = _compiled(policy.source_part_separators)
    for match in _compiled(policy.source_pattern).finditer(source):
        run = match.group(0).rstrip(",")
        values.add(_canonical(run))
        for part in splitter.split(run):
            if part:
                values.add(_canonical(part))
    words = {**policy.number_words, **policy.source_only_words}
    for match in _spelled_number_re(words, policy.compound_separator).finditer(source):
        value, _ = _spelled_value(match, words)
        values.add(str(value))
        # A compound in the source also grounds each of its words on its own.
        if match.group("unit") is not None:
            values.add(str(words[match.group("tens").lower()]))
            values.add(str(words[match.group("unit").lower()]))
    return values


def _is_marked_unverified(
    content: str, *, end: int, policy: ModelNumericGroundingPolicy
) -> bool:
    window = content[end : end + policy.unverified_window]
    return any(
        _compiled(marker, re.IGNORECASE).search(window)
        for marker in policy.unverified_markers
    )


def _claim_text(content: str, policy: ModelNumericGroundingPolicy) -> str:
    """The answer with its declared non-claim spans blanked, offsets kept."""
    for pattern in policy.excluded_answer_spans:
        content = _compiled(pattern, re.DOTALL).sub(
            lambda m: " " * len(m.group(0)), content
        )
    return content


def evaluate_numeric_grounding(
    *,
    content: str,
    grounding_source: str | None,
    policy: ModelNumericGroundingPolicy,
) -> ModelNumericGroundingVerdict:
    """Check every number ``content`` states against ``grounding_source``.

    ``None`` -- a gate input that carried no source -- yields an unevaluated
    verdict. A source that states no number does too when the contract says to
    skip it. The caller records either as a skipped check, never as a pass.
    """
    if grounding_source is None:
        return ModelNumericGroundingVerdict(evaluated=False)

    grounded = _source_values(grounding_source, policy)
    if (
        not grounded
        and policy.failure_policy.on_source_without_numbers == "skip_and_record"
    ):
        return ModelNumericGroundingVerdict(evaluated=False)

    terminator = (
        resolve_identifier_grounding_policy().answer_segment.stray_trace_terminator
    )
    answer = _claim_text(answer_segment(content, terminator=terminator), policy)
    checked = 0
    seen: set[str] = set()
    ungrounded: list[ModelUngroundedNumber] = []

    def _consider(value: str, as_written: str, end: int) -> None:
        nonlocal checked
        checked += 1
        if value in grounded or value in seen:
            return
        if _is_marked_unverified(answer, end=end, policy=policy):
            return
        seen.add(value)
        ungrounded.append(ModelUngroundedNumber(value=value, as_written=as_written))

    for match in _compiled(policy.claim_pattern).finditer(answer):
        digits = match.group(_CLAIM_GROUP)
        _consider(_canonical(digits), digits, match.end())

    for match in _spelled_number_re(
        policy.number_words, policy.compound_separator
    ).finditer(answer):
        if _compiled(policy.spelled_modifier_follower).match(answer, match.end()):
            continue
        value, as_written = _spelled_value(match, policy.number_words)
        _consider(str(value), as_written, match.end())

    return ModelNumericGroundingVerdict(
        evaluated=True,
        checked_count=checked,
        ungrounded=tuple(ungrounded[: policy.failure_policy.max_reported]),
    )


# ---------------------------------------------------------------------------
# names_resolve
# ---------------------------------------------------------------------------


def _python_blocks(content: str, policy: ModelNameResolutionPolicy) -> list[str]:
    """The Python the answer ships, read the way compiles_without_errors reads it."""
    fenced = _FENCE_WITH_LANG_RE.findall(content)
    if not fenced:
        return [_FENCE_RE.sub("", content)]
    tags = {tag.lower() for tag in policy.python_fence_tags}
    return [body for lang, body in fenced if lang.strip().lower() in tags]


def _free_names(code: str) -> set[str] | None:
    """Names the code reads that nothing in the code binds. None if unparseable.

    A module-scope symbol that is referenced but never assigned, imported or
    defined is free. Inside a function or class, a symbol the compiler resolved
    to the global scope is free unless the module binds it. A star import makes
    every name potentially bound, so the answer is then unresolvable and yields
    no finding rather than a guess.
    """
    try:
        top = symtable.symtable(code, "<delegated-answer>", "exec")
    except SyntaxError:
        return None
    if _STAR_IMPORT_RE.search(code):
        return set()
    bound = {
        symbol.get_name()
        for symbol in top.get_symbols()
        if symbol.is_assigned() or symbol.is_imported() or symbol.is_namespace()
    }
    referenced: set[str] = set()

    def _walk(table: symtable.SymbolTable) -> None:
        for symbol in table.get_symbols():
            if symbol.is_declared_global() and symbol.is_assigned():
                # ``global x`` then ``x = ...`` inside a function binds x.
                bound.add(symbol.get_name())
            if not symbol.is_referenced():
                continue
            if table.get_type() == "module" or symbol.is_global():
                referenced.add(symbol.get_name())
        for child in table.get_children():
            _walk(child)

    _walk(top)
    return referenced - bound


def evaluate_name_resolution(
    *,
    content: str,
    grounding_source: str | None,
    policy: ModelNameResolutionPolicy,
) -> ModelNameResolutionVerdict:
    """Find names the answer's Python reads that nothing binds or mentions.

    Unparseable code yields no finding here: ``compiles_without_errors`` owns
    that failure, and reporting it twice would double-charge one defect.
    """
    if grounding_source is None:
        return ModelNameResolutionVerdict(evaluated=False)

    terminator = (
        resolve_identifier_grounding_policy().answer_segment.stray_trace_terminator
    )
    answer = answer_segment(content, terminator=terminator)
    source_words = set(_compiled(policy.source_word_pattern).findall(grounding_source))
    known = _BUILTIN_NAMES | set(policy.implicit_module_names) | source_words
    unresolved: list[str] = []
    for block in _python_blocks(answer, policy):
        free = _free_names(block)
        if not free:
            continue
        for name in sorted(free - known):
            if name not in unresolved:
                unresolved.append(name)
    return ModelNameResolutionVerdict(
        evaluated=True,
        unresolved=tuple(unresolved[: policy.failure_policy.max_reported]),
    )


__all__: list[str] = [
    "evaluate_name_resolution",
    "evaluate_numeric_grounding",
    "resolve_name_resolution_policy",
    "resolve_numeric_grounding_policy",
]
