# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Classify what a DoD evidence check binds when it passes (OMN-15911).

Pure logic — no I/O, no env reads, no subprocess. Given the check mapping a
contract declares, decide whether passing it proves BEHAVIOR, merely binds
MERGE_STATE, stands in as a SURROGATE, or is INDETERMINATE.

Design rules, in priority order:

1. **Read the command, not the prose.** ``check_type`` and ``description`` are
   author-supplied and, per OMN-15391, are allowed to narrate a proof the
   command cannot produce. The single exception is ``check_type:
   file_exists``, which is not a command at all and is definitionally
   static-artifact inspection.
2. **Fail closed.** Anything unrecognized is INDETERMINATE, never BEHAVIOR.
   The consuming flip rule requires at least one behavior-proving check, so a
   misclassification toward BEHAVIOR releases a flip and a misclassification
   away from it merely holds one. Only the second direction is acceptable.
3. **The BEHAVIOR allowlist is tight and positive.** A command earns BEHAVIOR
   by naming a known test runner or the ONEX CLI, not by failing to look like
   anything else.
4. **The surrogate corpus lives in one place.** OMN-15391's
   ``omnimarket.occ_evidence_probative_class`` landed first and owns
   ``FOREIGN_SUITE_DENYLIST`` plus the bare-``gh pr view`` predicate; this
   module calls ``is_surrogate_check_value`` rather than keeping a second list
   that would drift out of step with it.

Relationship to OMN-15391, stated precisely because the two look alike: that
module asks whether a command's exit status *can* depend on the product change
(vacuity), this one asks what a check that *passed* actually bound (proof
strength). Neither subsumes the other. The case that separates them is the one
OMN-15391 records as deliberately out of its scope — an asserted merge probe,
``gh pr view <n> --json state --jq '.state' | grep -q MERGED``, is probative
there (it can go red) and is MERGE_STATE here (it proves a merge, not a
behavior).
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Final

from omnimarket.enums.enum_check_proof_class import EnumCheckProofClass
from omnimarket.occ_evidence_probative_class import is_surrogate_check_value

# Roll-up order for a multi-check evidence item: an item is VERIFIED only when
# EVERY one of its checks passed, so the strongest class among them is an
# honest label for what the item proved. MERGE_STATE outranks SURROGATE
# because it is at least bound to this ticket's own PR.
CHECK_PROOF_CLASS_PRECEDENCE: Final[tuple[EnumCheckProofClass, ...]] = (
    EnumCheckProofClass.BEHAVIOR,
    EnumCheckProofClass.MERGE_STATE,
    EnumCheckProofClass.SURROGATE,
    EnumCheckProofClass.INDETERMINATE,
)

# Test runners and product CLIs whose exit code is a statement about behavior.
_BEHAVIOR_WORDS: Final[frozenset[str]] = frozenset(
    {
        "pytest",
        "py.test",
        "tox",
        "nox",
        "unittest",
        # The ONEX CLI: running a node/skill executes the product itself.
        "onex",
        "vitest",
        "jest",
        "mocha",
        "rspec",
        "phpunit",
    }
)

# Runners that only mean "test" when paired with a test subcommand/target.
_BEHAVIOR_PAIRS: Final[tuple[tuple[str, str], ...]] = (
    ("go", "test"),
    ("cargo", "test"),
    ("npm", "test"),
    ("pnpm", "test"),
    ("yarn", "test"),
    ("bun", "test"),
    ("dotnet", "test"),
    ("gradle", "test"),
    ("mvn", "test"),
)

# `make <target>` is behavior only when the target names a test.
_MAKE_TEST_TARGET_RE: Final[re.Pattern[str]] = re.compile(r"(^|[-_.])test")

# Wrapper words to skip when finding a segment's real command head.
_WRAPPER_WORDS: Final[frozenset[str]] = frozenset(
    {
        "uv",
        "run",
        "poetry",
        "pipenv",
        "hatch",
        "pdm",
        "env",
        "sudo",
        "nice",
        "ionice",
        "time",
        "command",
        "exec",
        "--",
    }
)

_ENV_ASSIGNMENT_RE: Final[re.Pattern[str]] = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*=",
)

# Heads whose reads bind PR / merge / repo state.
_GH_MERGE_STATE_SUBCOMMANDS: Final[frozenset[str]] = frozenset(
    {"pr", "run", "release", "search"}
)
_GIT_MERGE_STATE_SUBCOMMANDS: Final[frozenset[str]] = frozenset(
    {
        "log",
        "rev-parse",
        "rev-list",
        "merge-base",
        "show",
        "ls-remote",
        "ls-files",
        "branch",
        "tag",
        "describe",
        "diff",
        "cat-file",
        "status",
    }
)

# Heads that only ever inspect a static artifact.
_STATIC_INSPECTION_HEADS: Final[frozenset[str]] = frozenset(
    {
        "test",
        "[",
        "ls",
        "cat",
        "grep",
        "egrep",
        "fgrep",
        "rg",
        "ag",
        "jq",
        "yq",
        "wc",
        "head",
        "tail",
        "find",
        "stat",
        "file",
        "sha256sum",
        "shasum",
        "md5sum",
        "diff",
        "cmp",
        "awk",
        "sed",
        "tr",
        "sort",
        "uniq",
        "cut",
        "basename",
        "dirname",
        "readlink",
        "realpath",
        "echo",
        "printf",
        "true",
        ":",
    }
)

# Which commands are surrogates is NOT decided here. OMN-15391 landed
# ``omnimarket.occ_evidence_probative_class`` first, and that module is the
# single definition of the surrogate corpus: ``FOREIGN_SUITE_DENYLIST`` (the
# ratcheted list of ticket-independent generic suites) and the bare-``gh pr
# view`` predicate. This module DELEGATES to it rather than keeping a second
# list that would drift.
#
# The two modules answer different questions and neither subsumes the other:
#
#   OMN-15391  "can this command's exit status depend on the product change?"
#              -> PROBATIVE / PR_STATE_SURROGATE / FOREIGN_SUITE_SURROGATE
#   OMN-15911  "what did this check BIND when it passed?"
#              -> behavior / merge-state / surrogate / indeterminate
#
# The residual OMN-15391 explicitly records as out of its scope is exactly what
# this axis catches: an ASSERTED merge probe
# (``gh pr view <n> --json state --jq '.state' | grep -q MERGED``) is probative
# by that module's definition — it can go red — and still proves only that a
# merge happened. It is MERGE_STATE here, and it can never release an autoclose
# flip.

# Shapes that discard a command's exit code, so the "proof" cannot fail.
_EXIT_CODE_LAUNDERING_RE: Final[re.Pattern[str]] = re.compile(
    r"(\|\||;|&&)\s*(true\b|:\s|:$|echo\b)|\|\s*true\b",
)

_SEGMENT_SPLIT_RE: Final[re.Pattern[str]] = re.compile(r"\|\||&&|\||;|\n")


def _words(text: str) -> list[str]:
    """Shell-split ``text``, degrading to whitespace split on malformed input."""
    try:
        return shlex.split(text)
    except ValueError:
        return text.split()


def _normalize_head(word: str) -> str:
    """Reduce ``/usr/local/bin/pytest`` / ``python3.12`` to a comparable head."""
    head = word.rsplit("/", 1)[-1]
    if head.startswith("python"):
        return "python"
    return head


def _segment_head_and_args(segment: str) -> tuple[str, list[str]]:
    """Return ``(head, args)`` for one pipeline segment, wrappers stripped."""
    words = _words(segment)
    index = 0
    while index < len(words):
        word = words[index]
        if _ENV_ASSIGNMENT_RE.match(word):
            index += 1
            continue
        normalized = _normalize_head(word)
        if normalized in _WRAPPER_WORDS:
            index += 1
            continue
        return normalized, words[index + 1 :]
    return "", []


def _python_module_head(args: Sequence[str]) -> str:
    """The module name in ``python -m <module>``, or an empty string."""
    for position, arg in enumerate(args):
        if arg == "-m" and position + 1 < len(args):
            return args[position + 1]
    return ""


def _is_behavior(head: str, args: Sequence[str]) -> bool:
    """True when this segment executes a test runner or the ONEX CLI."""
    if head in _BEHAVIOR_WORDS:
        return True
    if head == "python":
        module = _python_module_head(args)
        return module.split(".", 1)[0] in _BEHAVIOR_WORDS or module in _BEHAVIOR_WORDS
    if head == "make":
        return any(
            _MAKE_TEST_TARGET_RE.search(arg) for arg in args if not arg.startswith("-")
        )
    for runner, subcommand in _BEHAVIOR_PAIRS:
        if head == runner and subcommand in args:
            return True
    return False


def _is_merge_state(head: str, args: Sequence[str]) -> bool:
    """True when this segment only reads PR / merge / repo state."""
    if head == "gh":
        if not args:
            return False
        if args[0] in _GH_MERGE_STATE_SUBCOMMANDS:
            return True
        if args[0] == "api":
            # A GET against the pulls/commits surface. Any explicit non-GET
            # method is a mutation and is not a state read.
            if "--method" in args or "-X" in args:
                return False
            return any(
                "/pulls" in arg or "/commits" in arg or "/branches" in arg
                for arg in args
            )
        return False
    if head == "git":
        if not args:
            return False
        if args[0] == "grep":
            return False
        return args[0] in _GIT_MERGE_STATE_SUBCOMMANDS
    return False


def _is_static_inspection(head: str) -> bool:
    return head in _STATIC_INSPECTION_HEADS


def classify_command(command: str) -> EnumCheckProofClass:
    """Classify a shell command string. Pure; fails closed to INDETERMINATE."""
    text = command.strip()
    if not text:
        return EnumCheckProofClass.INDETERMINATE

    # OMN-15391's corpus first, and deliberately BEFORE the BEHAVIOR allowlist:
    # a denylisted foreign suite IS a real pytest run, so shape alone would
    # call it behavior. Delegating here keeps one definition of the surrogate
    # set across both lanes.
    if is_surrogate_check_value(text):
        return EnumCheckProofClass.SURROGATE

    # An exit code that cannot fail is not evidence of anything.
    if _EXIT_CODE_LAUNDERING_RE.search(text):
        return EnumCheckProofClass.INDETERMINATE

    segments = [
        segment.strip() for segment in _SEGMENT_SPLIT_RE.split(text) if segment.strip()
    ]
    if not segments:
        return EnumCheckProofClass.INDETERMINATE

    saw_merge_state = False
    saw_static = False
    for segment in segments:
        head, args = _segment_head_and_args(segment)
        if not head:
            return EnumCheckProofClass.INDETERMINATE
        if _is_behavior(head, args):
            return EnumCheckProofClass.BEHAVIOR
        if _is_merge_state(head, args):
            saw_merge_state = True
            continue
        if _is_static_inspection(head):
            saw_static = True
            continue
        return EnumCheckProofClass.INDETERMINATE

    if saw_merge_state:
        return EnumCheckProofClass.MERGE_STATE
    if saw_static:
        return EnumCheckProofClass.SURROGATE
    return EnumCheckProofClass.INDETERMINATE


def classify_check(check: Mapping[str, Any]) -> EnumCheckProofClass:
    """Classify one declared check mapping from a ``dod_evidence`` item."""
    check_type = check.get("check_type") or ""
    if check_type == "file_exists":
        # Not a command: a path existing is static-artifact inspection by
        # definition, whatever the item's prose claims it demonstrates.
        return EnumCheckProofClass.SURROGATE
    command = check.get("command") or check.get("check_value") or ""
    if not isinstance(command, str):
        return EnumCheckProofClass.INDETERMINATE
    return classify_command(command)


def classify_item_checks(checks: Iterable[Any]) -> EnumCheckProofClass:
    """Roll a ``dod_evidence`` item's checks up to a single class.

    An item is VERIFIED only when every one of its checks passed, so the
    strongest class present is an honest statement of what the item proved.
    An item with no usable checks is INDETERMINATE (it is SKIPPED or FAILED
    upstream and can never be behavior-proving).
    """
    found: set[EnumCheckProofClass] = set()
    for check in checks:
        if not isinstance(check, Mapping):
            found.add(EnumCheckProofClass.INDETERMINATE)
            continue
        found.add(classify_check(check))
    if not found:
        return EnumCheckProofClass.INDETERMINATE
    for candidate in CHECK_PROOF_CLASS_PRECEDENCE:
        if candidate in found:
            return candidate
    return EnumCheckProofClass.INDETERMINATE


__all__ = [
    "CHECK_PROOF_CLASS_PRECEDENCE",
    "classify_check",
    "classify_command",
    "classify_item_checks",
]
