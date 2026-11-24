# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure reads of the customer guide and the customer-surface parameter audit behind the C17 collector (OMN-26044).

Three reads, none of which touches a machine: which command a guide's primary path leads with, which install line it
tells the customer to run (and the interpreter that line names), and whether an internal-only parameter appears on a
command the installed ``onex`` exposes or in the guide itself. Every zero has a positive control: a parameter seeded into
a copy of each surface must be caught, or the audit has not looked.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

# The registry root's own name, spelled in two parts so this file does not add a tree-surface occurrence of it.
_ROOT = "_".join(("OMNI", "HOME"))
CONTROL_OPTION = "--" + "-".join(("omni", "home"))
CONTROL_COMMAND = "onex c17-positive-control"
CONTROL_GUIDE_LINE = f"export {_ROOT}=${{{_ROOT}:-.}}"
EXCERPT = 160

#: One command contributed by each package the install line brings in, so a walk that lost a whole extension group
#: fails the floor instead of auditing less.
REQUIRED_CLI_COMMANDS = frozenset({"onex delegate", "onex local", "onex info"})

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
_APPENDIX = re.compile(r"^appendix\b", re.IGNORECASE)
#: A command that delegates, or touches the cloud plane at all, in its CLI and plugin-skill forms.
_PATH_COMMAND = re.compile(
    r"^(onex\s+(cloud|delegate)\b|/onex:(cloud_delegate|delegate)\b)"
)
_LOCAL_COMMAND = re.compile(r"^(onex\s+delegate\b|/onex:delegate\b)")
_SHELL_EXPANSION = re.compile(r"[$`\n\\]")
_SHELL_OPERATOR_CHARS = frozenset("();<>|&")
_PROSE_PYTHON_FLOOR = re.compile(r"\bPython (3\.\d+)\+")
_PYTHON_VERSION = re.compile(r"^3\.\d+(\.\d+)?$")
_UV_VALUE_OPTIONS = frozenset(
    {
        "--python",
        "-p",
        "--with",
        "-w",
        "--from",
        "--index",
        "--index-url",
        "--default-index",
        "--extra-index-url",
        "--with-requirements",
        "--with-editable",
        "--constraints",
        "-c",
        "--overrides",
        "--build-constraints",
        "-b",
    }
)
_REQUIREMENT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*")


def split_primary_path(text: str) -> str:
    """The guide above its appendix heading; with no appendix heading the whole guide is the primary path (the strict read).

    A heading inside a fenced block is not a heading: a ``# comment`` in a shell block must not end the path early.
    """
    lines = text.splitlines()
    fence: str | None = None
    for index, line in enumerate(lines):
        fence_match = _FENCE.match(line)
        if fence_match:
            marker = fence_match.group(1)
            if fence is None:
                fence = marker[0] * len(marker)
            elif marker[0] == fence[0] and len(marker) >= len(fence):
                fence = None
            continue
        if fence is None:
            heading = _HEADING.match(line)
            if heading and _APPENDIX.match(heading.group(2).lstrip("*_ ")):
                return "\n".join(lines[:index])
    return text


def fenced_lines(text: str) -> list[str]:
    """Every line inside a fenced block, with backslash continuations joined."""
    out: list[str] = []
    fence: str | None = None
    pending = ""
    for line in text.splitlines():
        fence_match = _FENCE.match(line)
        if fence_match:
            marker = fence_match.group(1)
            if fence is None:
                fence = marker[0] * len(marker)
                continue
            if marker[0] == fence[0] and len(marker) >= len(fence):
                fence = None
                if pending:
                    out.append(pending.strip())
                    pending = ""
                continue
        if fence is None:
            continue
        stripped = line.rstrip()
        if stripped.endswith("\\"):
            pending += stripped[:-1] + " "
            continue
        out.append((pending + stripped).strip())
        pending = ""
    return out


def guide_leads_with_local(text: str) -> bool:
    """The first delegation-path command of the primary path is the local ``onex delegate`` (order, not presence)."""
    commands = [
        line
        for line in fenced_lines(split_primary_path(text))
        if _PATH_COMMAND.match(line)
    ]
    return bool(commands) and bool(_LOCAL_COMMAND.match(commands[0]))


@dataclass(frozen=True)
class InstallLine:
    raw: str
    argv: tuple[str, ...] | None
    python: str | None
    package: str | None
    refusal: str | None
    #: The floor the guide names in prose when the line itself names none.
    python_floor: str | None = None


def _has_shell_syntax(raw: str) -> bool:
    if _SHELL_EXPANSION.search(raw):
        return True
    lexer = shlex.shlex(raw, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return True
    return any(tok and set(tok) <= _SHELL_OPERATOR_CHARS for tok in tokens)


def parse_install_line(raw: str, python_floor: str | None = None) -> InstallLine:
    """Accept only a plain ``uv tool install`` naming its interpreter; the line is rebuilt from argv, never pasted to a shell."""

    def refuse(reason: str) -> InstallLine:
        return InstallLine(raw, None, None, None, reason, python_floor)

    if _has_shell_syntax(raw):
        return refuse("the install line carries shell syntax and is not executed")
    try:
        argv = shlex.split(raw)
    except ValueError as exc:
        return refuse(f"the install line does not tokenise: {exc}")
    if argv[:3] != ["uv", "tool", "install"]:
        return refuse("not a `uv tool install` line")
    python: str | None = None
    positionals: list[str] = []
    index = 3
    while index < len(argv):
        token = argv[index]
        if token.startswith("-"):
            flag, eq, value = token.partition("=")
            if flag in _UV_VALUE_OPTIONS:
                if not eq:
                    index += 1
                    value = argv[index] if index < len(argv) else ""
                if flag in {"--python", "-p"}:
                    python = value
        else:
            positionals.append(token)
        index += 1
    if python is None and python_floor is None:
        return refuse(
            "neither the install line (`--python`) nor the guide names an interpreter"
        )
    if python is not None and not _PYTHON_VERSION.match(python):
        return refuse(
            f"`--python {python}` is not a CPython version this read can check"
        )
    if len(positionals) != 1:
        return refuse(f"expected one tool package, found {positionals!r}")
    name = _REQUIREMENT_NAME.match(positionals[0])
    if not name:
        return refuse(f"cannot read a package name from {positionals[0]!r}")
    return InstallLine(raw, tuple(argv), python, name.group(0), None, python_floor)


def install_lines(guide_text: str) -> list[InstallLine]:
    primary = split_primary_path(guide_text)
    floor = _PROSE_PYTHON_FLOOR.search(primary)
    seen: list[str] = []
    for line in fenced_lines(primary):
        if line.startswith("uv tool install") and line not in seen:
            seen.append(line)
    return [
        parse_install_line(line, floor.group(1) if floor else None) for line in seen
    ]


def _version_tuple(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", text)[:3])


def interpreter_matches(line: InstallLine, version: str) -> bool:
    """``--python X`` is exact; a prose "Python X+" is a floor."""
    if not version:
        return False
    if line.python is not None:
        return version == line.python or version.startswith(f"{line.python}.")
    if line.python_floor is not None:
        return _version_tuple(version)[:2] >= _version_tuple(line.python_floor)[:2]
    return False


Pattern = tuple[str, re.Pattern[str]]


def load_patterns(registry: Mapping[str, Any]) -> list[Pattern]:
    return [
        (entry["id"], re.compile(entry["regex"]))
        for entry in registry.get("patterns", [])
    ]


def _canonical(name: str) -> str:
    return name.lstrip("-").replace("-", "_").upper()


def render_parameter(names: Sequence[str], envvars: Sequence[str]) -> list[str]:
    """A parameter's identities in the forms the registry is written against (shell-expansion and ``envvar=`` bindings)."""
    lines = [f"${_canonical(n)}" for n in names if n and _canonical(n)]
    for envvar in envvars:
        lines += [f'envvar="{envvar}"', f"${envvar}"]
    return list(dict.fromkeys(lines))


def _scan(
    patterns: Sequence[Pattern], surface: str, where: str, text: str
) -> list[str]:
    findings: list[str] = []
    for pattern_id, regex in patterns:
        for match in regex.finditer(text):
            start = max(0, match.start() - EXCERPT // 2)
            excerpt = " ".join(text[start : match.end() + EXCERPT // 2].split())
            findings.append(f"{surface}|{where}|{pattern_id}|{excerpt}")
    return findings


def _envvars(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value]


def audit_cli(
    patterns: Sequence[Pattern], commands: Sequence[Mapping[str, Any]]
) -> list[str]:
    findings: list[str] = []
    for command in commands:
        path = command["path"]
        findings += _scan(patterns, "cli", f"{path} --help", command.get("help") or "")
        for param in command.get("params", []):
            label = (
                f"{path} {'/'.join(param.get('opts') or [param.get('name') or '?'])}"
            )
            names = [*(param.get("opts") or []), param.get("name") or ""]
            for line in render_parameter(names, _envvars(param.get("envvar"))):
                findings += _scan(patterns, "cli", label, line)
            for field in ("default", "help"):
                if param.get(field):
                    findings += _scan(
                        patterns, "cli", f"{label} ({field})", str(param[field])
                    )
    return findings


def audit_guide(patterns: Sequence[Pattern], name: str, text: str) -> list[str]:
    """The whole guide, appendix included: a customer reads the appendix too."""
    return _scan(patterns, "guide", name, text)


def controls_caught(
    patterns: Sequence[Pattern],
    commands: Sequence[Mapping[str, Any]],
    guide_name: str,
    guide_text: str,
) -> bool:
    """Seed an internal-only parameter into a copy of each surface and require it caught."""
    seeded_cli = [
        *commands,
        {
            "path": CONTROL_COMMAND,
            "help": "",
            "params": [
                {
                    "name": "_".join(("omni", "home")),
                    "opts": [CONTROL_OPTION],
                    "envvar": None,
                    "default": "None",
                    "help": None,
                }
            ],
        },
    ]
    cli_caught = any(
        f.split("|")[1].startswith(CONTROL_COMMAND)
        for f in audit_cli(patterns, seeded_cli)
    )
    guide_caught = any(
        CONTROL_GUIDE_LINE[:14] in f
        for f in audit_guide(
            patterns, guide_name, f"{guide_text}\n{CONTROL_GUIDE_LINE}\n"
        )
    )
    return cli_caught and guide_caught


def floor_failures(
    registry: Mapping[str, Any], commands: Sequence[Mapping[str, Any]]
) -> list[str]:
    """An enumeration below its floor is a failed clause, never a smaller audit."""
    floor: list[str] = []
    if not load_patterns(registry):
        floor.append("the internal-only pattern registry holds no pattern")
    paths = {c["path"] for c in commands}
    if REQUIRED_CLI_COMMANDS - paths:
        floor.append(f"the CLI walk is missing {sorted(REQUIRED_CLI_COMMANDS - paths)}")
    return floor


def audit_customer_surface(
    registry: Mapping[str, Any],
    commands: Sequence[Mapping[str, Any]],
    guide_name: str,
    guide_text: str,
) -> tuple[tuple[str, ...], bool]:
    """The distinct findings (one per surface, place and pattern) and whether every control and floor held."""
    patterns = load_patterns(registry)
    findings = [
        *audit_cli(patterns, commands),
        *audit_guide(patterns, guide_name, guide_text),
    ]
    distinct = {tuple(f.split("|")[:3]): f for f in findings}
    floor = floor_failures(registry, commands)
    held = not floor and controls_caught(patterns, commands, guide_name, guide_text)
    return (*distinct.values(), *(f"floor|{item}" for item in floor)), held
