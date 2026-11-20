# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Mirror the hosted deploy gate's live-probe classifier before push.

Ported from omniclaude's validate_pr_deploy_required.py at commit 61761f04.
Keep the classifier in sync with that pinned source: in particular, ``uv`` is
not a transparent wrapper, so ``uv run onex ...`` resolves to ``uv`` and fails.
Pure: no I/O, command execution, or rollout/grandfather exemptions.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from re import Pattern
from typing import Any, Final

__all__ = ["contract_check_values", "is_hosted_falsifiable"]

# Commands that probe a live surface: their exit status depends on the state of a
# deployed system. ``gh-api`` is the compound token ``_commands_in`` adds for the
# ``gh`` CLI's ``api`` verb; bare ``gh`` is not a probe.
_LIVE_PROBE_NAMES: Final[str] = (
    "docker,docker-compose,podman,nerdctl,kubectl,rpk,kcat,kafkacat,psql,mysql,"
    "redis-cli,valkey-cli,mongosh,curl,wget,httpie,ssh,onex,gh-api"
)
LIVE_PROBE_COMMANDS: Final[frozenset[str]] = frozenset(_LIVE_PROBE_NAMES.split(","))

# Transparent wrappers: they execute their argument, so the real command is the
# next token. `env -u PYTHONPATH docker exec ...` must still resolve to `docker`.
WRAPPER_COMMANDS: Final[frozenset[str]] = frozenset(
    {
        "env",
        "sudo",
        "time",
        "timeout",
        "nohup",
        "stdbuf",
        "nice",
        "command",
        "exec",
        "xargs",
        "then",
        "do",
    }
)

# Shells whose `-c <string>` argument is itself a script to parse.
SHELL_COMMANDS: Final[frozenset[str]] = frozenset({"bash", "sh", "zsh", "dash"})

# Artifact trees the evidence author writes in the SAME OCC companion PR. A
# check_value that reads these back is self-referential: its verdict is decided by
# text the author typed, not by the state of any deployed system.
SELF_REFERENTIAL_PATTERNS: Final[list[Pattern[str]]] = [
    # the DoD receipt tree — drift/dod_receipts/OMN-X/<item>/command.yaml
    re.compile(r"dod_receipts", re.IGNORECASE),
    # $CONTRACT_REPO_DIR / ${CONTRACT_REPO_DIR} — the OCC checkout root
    re.compile(r"\$\{?CONTRACT_REPO_DIR\b", re.IGNORECASE),
    # the ticket's own contract file
    re.compile(r"contracts/OMN-\d+\.ya?ml", re.IGNORECASE),
]

_CMD_SUBST_RE: Final[Pattern[str]] = re.compile(r"\$\(([^()]*(?:\([^()]*\)[^()]*)*)\)")
_BACKTICK_RE: Final[Pattern[str]] = re.compile(r"`([^`]*)`")
_ASSIGNMENT_RE: Final[Pattern[str]] = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# Shell operators that terminate one command and begin the next. Split on these
# only AFTER lexing — a `;` inside a quoted python -c string is data, not an
# operator, and splitting the raw text on it corrupts the quoting.
_OPERATOR_TOKENS: Final[frozenset[str]] = frozenset(
    {"&&", "||", "|", ";", "&", "(", ")", "\n"}
)

# Wrapper flags that consume the following token as their value, so the real
# command is two tokens further on (`env -u PYTHONPATH docker exec ...`).
_WRAPPER_FLAGS_WITH_ARG: Final[frozenset[str]] = frozenset(
    {"-u", "--unset", "-C", "--chdir", "-S", "--split-string"}
)

# `timeout 30`, `timeout 1m` — a bare duration argument to a wrapper.
_DURATION_RE: Final[Pattern[str]] = re.compile(r"^\d+(\.\d+)?[smhd]?$")


@dataclass(frozen=True)
class CheckVerdict:
    """Why one check_value was accepted or rejected as deploy evidence."""

    falsifiable: bool
    reason: str


def _lex(script: str) -> list[str]:
    """Lex a shell script into tokens, with operators kept as their own tokens.

    ``punctuation_chars=True`` is what makes this correct: it groups ``&&`` / ``||``
    as single tokens AND — critically — it lexes *after* quote handling, so a ``;``
    inside ``python -c "a; b"`` stays inside the quoted token instead of being
    mistaken for a command separator.

    Quote-stripping is also what makes the whole rule work: ``echo 'docker exec x'``
    lexes to ``['echo', 'docker exec x']``, so the probe name inside the quotes is a
    single *argument* token and can never reach command position.

    An unparseable script (unbalanced quotes) fails CLOSED — no tokens, no probe.
    """
    lexer = shlex.shlex(script, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        return list(lexer)
    except ValueError:
        return []


def _command_of(tokens: list[str]) -> tuple[str | None, list[str]]:
    """Return (command, remaining_args) for one pipeline segment.

    Skips leading ``!`` negation, ``VAR=value`` assignments, and transparent
    wrappers (``env -u PYTHONPATH docker exec ...`` must still resolve to
    ``docker``; ``timeout 30 kubectl ...`` to ``kubectl``).

    ``command -v``/``command -V`` is a POSIX PATH-lookup builtin, not
    execution: ``command -v docker`` only tests that ``docker`` resolves to
    an executable, so its exit status carries no information about whether
    docker itself would succeed. Unwrapping it to ``docker`` (as with a real
    wrapper like ``env``) would let a lookup-only check pass as a live probe
    — the same vacuous-acceptance defect this file exists to close. Detected
    before generic wrapper-flag consumption so it can short-circuit to "no
    command" instead of falling through to the wrapped executable.
    """
    idx = 0
    in_wrapper = False
    while idx < len(tokens):
        word = tokens[idx]
        if word == "!" or _ASSIGNMENT_RE.match(word):
            idx += 1
            continue
        if Path(word).name.lower() in WRAPPER_COMMANDS:
            if (
                Path(word).name.lower() == "command"
                and idx + 1 < len(tokens)
                and (tokens[idx + 1] in ("-v", "-V"))
            ):
                return None, []
            in_wrapper = True
            idx += 1
            continue
        if in_wrapper:
            # Consume the wrapper's own flags/arguments until the real command.
            if word in _WRAPPER_FLAGS_WITH_ARG:
                idx += 2
                continue
            if word.startswith("-") or _DURATION_RE.match(word):
                idx += 1
                continue
        break
    if idx >= len(tokens):
        return None, []
    return Path(tokens[idx]).name.lower(), tokens[idx + 1 :]


def _commands_in(script: str, depth: int = 0) -> set[str]:
    """Return the commands appearing in COMMAND POSITION in a shell script.

    Recurses into ``$(...)`` / backtick substitutions and ``bash -c '<script>'``,
    because a probe genuinely executes in all three positions. Depth-bounded so
    pathological nesting cannot spin.
    """
    if depth > 4 or not script.strip():
        return set()

    found: set[str] = set()

    # Commands inside substitutions ARE executed — recurse before stripping them.
    for match in _CMD_SUBST_RE.finditer(script):
        found |= _commands_in(match.group(1), depth + 1)
    for match in _BACKTICK_RE.finditer(script):
        found |= _commands_in(match.group(1), depth + 1)

    # Replace substitutions with a space so their inner text is not re-read as a
    # top-level argument. Substituting a space (not "") keeps surrounding quotes
    # balanced: `test "$(docker ...)" = x` -> `test " " = x`.
    stripped = _BACKTICK_RE.sub(" ", _CMD_SUBST_RE.sub(" ", script))

    # Split the LEXED token stream on operator tokens — never the raw text.
    segment: list[str] = []
    segments: list[list[str]] = []
    for token in _lex(stripped):
        if token in _OPERATOR_TOKENS:
            segments.append(segment)
            segment = []
        else:
            segment.append(token)
    segments.append(segment)

    for tokens in segments:
        command, rest = _command_of(tokens)
        if command is None:
            continue

        # `bash -c '<script>'` — the -c argument is a script, not an argument.
        if command in SHELL_COMMANDS:
            if "-c" in rest:
                c_at = rest.index("-c")
                if c_at + 1 < len(rest):
                    found |= _commands_in(rest[c_at + 1], depth + 1)
            continue

        found.add(command)

        # The ``api`` verb of ``gh`` reads repository content at a ref; bare ``gh``
        # (``gh pr view`` and the like) only reports metadata and is not a probe.
        if command == "gh" and rest and rest[0] == "api":
            found.add("gh-api")

    return found


def classify_check_value(value: object) -> CheckVerdict:
    """Decide whether a check_value is a FALSIFIABLE deploy probe.

    Falsifiable means: run it against un-deployed / unfixed code and it goes RED.
    Fails CLOSED — anything not provably a live probe is rejected.
    """
    if not isinstance(value, str) or not value.strip():
        return CheckVerdict(False, "check_value is empty or not a string")

    # (1) DENY self-reference FIRST and unconditionally. This is the rule that
    # defeats embedding a probe keyword as quoted text beside a circular grep.
    for pattern in SELF_REFERENTIAL_PATTERNS:
        if pattern.search(value):
            return CheckVerdict(
                False,
                "self-referential: reads back the receipt/contract tree the "
                "evidence author writes in the same PR, so its verdict is decided "
                "by text the author typed, not by the state of a deployed system "
                f"(matched {pattern.pattern!r})",
            )

    # (2) ALLOW only a live-surface probe in COMMAND POSITION.
    commands = _commands_in(value)
    probes = commands & LIVE_PROBE_COMMANDS
    if not probes:
        return CheckVerdict(
            False,
            "no live-surface probe in command position — the check's exit status "
            "cannot depend on the state of the deployed system (commands found: "
            f"{sorted(commands) or 'none'}; expected one of: "
            f"{sorted(LIVE_PROBE_COMMANDS)})",
        )

    return CheckVerdict(
        True, f"live-surface probe in command position: {sorted(probes)}"
    )


def is_hosted_falsifiable(check_value: object) -> bool:
    """Return whether the pinned hosted classifier accepts this check value."""
    return classify_check_value(check_value).falsifiable


def contract_check_values(items: list[Any]) -> list[str]:
    """Collect every check_value string from the contract's dod_evidence items."""
    values: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        checks = item.get("checks")
        if not isinstance(checks, list):
            continue
        for check in checks:
            if not isinstance(check, dict):
                continue
            value = check.get("check_value")
            if isinstance(value, str):
                values.append(value)
    return values
