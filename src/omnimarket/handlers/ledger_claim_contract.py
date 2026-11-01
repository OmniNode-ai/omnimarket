# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Flag-gated CLAIM contract floor for the ledger append path.

The authoritative classifier is omnimarket node_dod_verify check_proof_class.
This is a deliberately narrower, fail-closed floor for the append path, not
verification that a declared check has run or passed. It uses only the stdlib.
Core's renderer replaces pipes inside dod-evidence JSON with broken bars;
those remain ordinary characters in a command string here.
"""

from __future__ import annotations

import json
import os
import re
import shlex
from collections.abc import Mapping

REQUIRE_CLAIM_CONTRACT_ENV = "ONEX_LEDGER_REQUIRE_CLAIM_CONTRACT"

# These are the only non-lane CLAIM writers in the fleet: the landing
# controller's relay CLAIM, the remote-lane runner's CLAIM, and the unattended
# morning worktree prune's CLAIM. Match the actor value exactly: no prefix,
# no model=none, no file. A new script writer must be added here in code,
# in a reviewed change.
EXEMPT_CLAIM_ACTORS = frozenset(
    {
        "script:landing-controller",
        "script:onex-remote-lane",
        "launchd:morning-worktree-prune",
    }
)

CHECK_TYPES = frozenset(
    {
        "test_exists",
        "test_passes",
        "file_exists",
        "grep",
        "command",
        "endpoint",
        "behavior_proven",
        "rendered_output",
        "runtime_sha_match",
        "command_exit_0",
        "semantic_grading",
        "disposition",
    }
)
BEHAVIOR_CHECK_TYPES = frozenset(
    {
        "test_passes",
        "behavior_proven",
        "endpoint",
        "rendered_output",
        "runtime_sha_match",
    }
)
BEHAVIOR_COMMANDS = frozenset(
    {"pytest", "py.test", "tox", "nox", "vitest", "jest", "onex"}
)
_ASSIGNMENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*", re.DOTALL)
_SCHEMA_RE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")


def enforced(environ: Mapping[str, str] | None = None) -> bool:
    """Opt in only for 1/true/yes/on; absent and unknown values are OFF."""
    source = os.environ if environ is None else environ
    return source.get(REQUIRE_CLAIM_CONTRACT_ENV, "").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def exempt_actor(fields: dict[str, list[str]]) -> bool:
    """Exempt only a single actor cell with an exact fleet writer value."""
    actors = fields.get("actor", [])
    return len(actors) == 1 and actors[0] in EXEMPT_CLAIM_ACTORS


def _behavior_command(command: str) -> bool:
    """Recognize a runner after setup prefixes; never execute the command.

    Unknown env options or uv options are deliberately not inferred. A later
    runner behind a readback command does not make that command behavior proof.
    """
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>()")
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return False
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if _ASSIGNMENT_RE.fullmatch(token):
            i += 1
        elif token == "env":
            i += 1
            while i < len(tokens) and tokens[i].startswith("-"):
                option = tokens[i]
                if option == "--":
                    i += 1
                    break
                if option in {"-i", "--ignore-environment", "-0", "--null"}:
                    i += 1
                elif option in {"-u", "--unset", "-C", "--chdir"}:
                    if i + 1 >= len(tokens) or tokens[i + 1] in {"&&", ";", "|"}:
                        return False
                    i += 2
                elif option.startswith(("--unset=", "--chdir=")):
                    if not option.partition("=")[2]:
                        return False
                    i += 1
                else:
                    return False
        elif token == "cd":
            if i + 2 >= len(tokens) or tokens[i + 2] != "&&":
                return False
            if not tokens[i + 1] or tokens[i + 1].startswith("-"):
                return False
            i += 3
        elif token == "uv":
            if tokens[i : i + 2] != ["uv", "run"]:
                return False
            i += 2
            if i < len(tokens) and tokens[i] == "--":
                i += 1
        else:
            return token in BEHAVIOR_COMMANDS
    return False


def _nonempty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _check_value_valid(value: object) -> bool:
    if isinstance(value, str):
        return _nonempty_string(value)
    return (
        isinstance(value, dict)
        and bool(value)
        and all(
            isinstance(key, str) and isinstance(item, str)
            for key, item in value.items()
        )
    )


def claim_refusal(fields: dict[str, list[str]]) -> str | None:
    """Pure refusal for Row.fields; callers decide whether enforcement is on."""
    evidence = fields.get("dod-evidence", [])
    if not evidence:
        return "missing dod-evidence=: add a non-empty JSON array of contract items"
    if len(evidence) != 1:
        return "dod-evidence= occurs more than once; supply exactly one cell"
    if not evidence[0].strip():
        return "dod-evidence= is empty; supply a non-empty JSON array"
    try:
        items = json.loads(evidence[0])
    except (ValueError, RecursionError):
        return "dod-evidence= is malformed JSON; supply a JSON array"
    if not isinstance(items, list):
        return "dod-evidence= must be a JSON array"
    if not items:
        return "dod-evidence= is an empty array; supply at least one contract item"

    behavior_proving = False
    for index, item in enumerate(items):
        location = f"dod-evidence[{index}]"
        if not isinstance(item, dict):
            return f"{location} must be an object"
        for name in ("id", "description"):
            if not _nonempty_string(item.get(name)):
                return f"{location}.{name} must be a non-empty string"
        checks = item.get("checks")
        if not isinstance(checks, list):
            return f"{location}.checks must be a list"
        for check_index, check in enumerate(checks):
            check_location = f"{location}.checks[{check_index}]"
            if not isinstance(check, dict):
                return f"{check_location} must be an object"
            kind = check.get("check_type")
            if not isinstance(kind, str) or kind not in CHECK_TYPES:
                return f"{check_location}.check_type is invalid; expected a supported check_type"
            value = check.get("check_value")
            if not _check_value_valid(value):
                return (
                    f"{check_location}.check_value must be a non-empty string "
                    "or non-empty object of string values"
                )
            if kind in BEHAVIOR_CHECK_TYPES or (
                kind in {"command", "command_exit_0"}
                and isinstance(value, str)
                and _behavior_command(value)
            ):
                behavior_proving = True

    schemas = fields.get("contract-schema", [])
    if len(schemas) != 1 or _SCHEMA_RE.fullmatch(schemas[0]) is None:
        return "contract-schema= must occur exactly once with major.minor.patch digits"
    if not behavior_proving:
        return (
            "dod-evidence= needs a behavior-proving item; "
            "static checks and readbacks do not prove behavior"
        )
    return None
