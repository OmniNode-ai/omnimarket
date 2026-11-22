# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The served curl installer against the candidate manifest (OMN-25070 AC4).

The script must be the one the manifest pins by sha256, assign the manifest's customer package and
its version exactly once each, name no package index other than PyPI and no find-links source but a
plain shell variable, change no build setting through the environment, and run every install or
run command with the exact --no-build flag. It reads the script as a list of refused spellings,
not as a shell: an install hidden behind eval, sh -c, a sourced file or a function, or a package
given as a URL, path or git reference, is not detected. A mismatch is a finding that names the
line; nothing raises.
"""

from __future__ import annotations

import hashlib
import re
import shlex

from omnimarket.nodes.node_release_candidate_verify_effect.models.enum_release_candidate_finding import (
    EnumReleaseCandidateFinding as K,
)
from omnimarket.nodes.node_release_candidate_verify_effect.models.model_release_candidate_manifest import (
    ModelReleaseCandidateManifest,
)
from omnimarket.nodes.node_release_candidate_verify_effect.models.model_release_candidate_verify_result import (
    ModelReleaseCandidateFinding,
)

PYPI_INDEXES = frozenset({"https://pypi.org/simple", "https://pypi.org/simple/"})
_ASSIGNMENT = re.compile(
    r"^(?:(?:export|readonly|declare(?:\s+-[a-zA-Z]+)*|local)\s+)?"
    r"(ONEX_PACKAGE|ONEX_VERSION)\+?=(\S*)"
)
_INDEX_FLAGS = frozenset(
    {
        "--index-url",
        "--extra-index-url",
        "--default-index",
        "--index",
        "--find-links",
        "--config-file",
        "--index-strategy",
    }
)
_INDEX_VARIABLE = re.compile(
    r"\b(UV_INDEX_URL|UV_EXTRA_INDEX_URL|UV_DEFAULT_INDEX|UV_INDEX|UV_FIND_LINKS|"
    r"UV_CONFIG_FILE|UV_INDEX_STRATEGY|PIP_INDEX_URL|PIP_EXTRA_INDEX_URL|PIP_FIND_LINKS|"
    r"PIP_CONFIG_FILE)=(\S*)"
)
_BUILD_VARIABLE = re.compile(
    r"\b(UV_NO_BUILD\w*|UV_NO_BINARY\w*|PIP_NO_BINARY|PIP_ONLY_BINARY|"
    r"PIP_NO_BUILD_ISOLATION)=(\S*)"
)
_SHORT_INDEX_FLAGS = ("-i", "-f")
_WORD_START = frozenset(";&|()")
_VARIABLE_VALUE = re.compile(r"^\$\{?[A-Za-z_]\w*\}?$")
_NO_BUILD_TOKENS = frozenset({"--no-build", "--only-binary=:all:"})
_SEPARATORS = frozenset({";", "&&", "||", "|", "&", "(", ")", "{", "}"})
_INSTALL_TOOL = re.compile(r"pip[0-9.]*|pipx|tool|uv")
_UV_SUBCOMMANDS = frozenset({"add", "sync", "run"})


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _finding(kind: K, subject: str, detail: str = "") -> ModelReleaseCandidateFinding:
    return ModelReleaseCandidateFinding(kind=kind, subject=subject, detail=detail)


def _continues(line: str) -> bool:
    """True when the line ends in an unescaped backslash, so the shell joins it with the next line; an even run of trailing backslashes is literal text."""
    run = len(line) - len(line.rstrip("\\"))
    return run % 2 == 1


def _logical_lines(script: str) -> list[tuple[int, str]]:
    lines = script.splitlines()
    result: list[tuple[int, str]] = []
    i = 0
    n = len(lines)
    while i < n:
        start = i
        # A comment ends at its newline: a backslash inside it does not continue the line.
        text = _strip_comment(lines[i])
        continued = text == lines[i] and _continues(text)
        while continued and i + 1 < n:
            i += 1
            following = _strip_comment(lines[i])
            text = text[:-1] + " " + following
            continued = following == lines[i] and _continues(following)
        i += 1
        stripped = text.strip()
        if stripped and not stripped.startswith("#"):
            result.append((start + 1, stripped))
    return result


def _strip_comment(line: str) -> str:
    """Cut a shell comment: a # outside quotes at the start of a word, as the shell reads it.

    A # inside a word (a#b, $#, ${#1}, a URL fragment) is text, not a comment.
    """
    quote = ""
    escaped = False
    previous = ""
    for index, char in enumerate(line):
        if escaped:
            escaped = False
        elif char == "\\" and quote != "'":
            escaped = True
        elif quote:
            if char == quote:
                quote = ""
        elif char in "'\"":
            quote = char
        elif char == "#" and (
            not previous or previous.isspace() or previous in _WORD_START
        ):
            return line[:index]
        previous = char
    return line


def _tokens(line: str) -> list[str]:
    code = _strip_comment(line)
    try:
        lex = shlex.shlex(code, posix=True, punctuation_chars=";&|(){}")
        lex.whitespace_split = True
        lex.commenters = ""
        return list(lex)
    except ValueError:
        return code.split()


def _segments(tokens: list[str]) -> list[list[str]]:
    segments: list[list[str]] = []
    current: list[str] = []
    for token in tokens:
        if token in _SEPARATORS:
            if current:
                segments.append(current)
                current = []
        else:
            current.append(token)
    if current:
        segments.append(current)
    return segments


def _basename(token: str) -> str:
    stripped = token.strip("\"'")
    return stripped.rsplit("/", 1)[-1]


def _is_install(segment: list[str]) -> bool:
    for i, token in enumerate(segment):
        if (
            token == "install"
            and i > 0
            and _INSTALL_TOOL.match(_basename(segment[i - 1]))
        ):
            return True
        if _basename(token) == "uvx":
            return True
        if (
            (token == "uv" or token.endswith("/uv") or token == "$uv")
            and i + 1 < len(segment)
            and segment[i + 1] in _UV_SUBCOMMANDS
        ):
            return True
    for i in range(len(segment) - 1):
        if segment[i] == "tool" and segment[i + 1] == "run":
            return True
    return False


def _index_flags(tokens: list[str]) -> list[tuple[str, str]]:
    """Every index or package-source flag with its value, as `flag value` or `flag=value`.

    The short forms -i and -f (also attached, as in -ihttps://...) count only in an install
    command or a `set --` that builds one, so a `[ -f file ]` test is not a find-links flag.
    """
    pairs: list[tuple[str, str]] = []
    for segment in _segments(tokens):
        short = _is_install(segment) or segment[0] == "set"
        i = 0
        while i < len(segment):
            token = segment[i]
            if short and token in _SHORT_INDEX_FLAGS:
                pairs.append((token, segment[i + 1] if i + 1 < len(segment) else ""))
                i += 2
                continue
            if short and token[:2] in _SHORT_INDEX_FLAGS and len(token) > 2:
                pairs.append((token[:2], token[2:]))
                i += 1
                continue
            if token in _INDEX_FLAGS:
                pairs.append((token, segment[i + 1] if i + 1 < len(segment) else ""))
                i += 2
                continue
            flag, _, value = token.partition("=")
            if value and flag in _INDEX_FLAGS:
                pairs.append((flag, value))
            i += 1
    return pairs


def _refused_source(flag: str, value: str) -> bool:
    """--index-strategy and --config-file are always refused; find-links only from a plain variable."""
    if flag in ("--index-strategy", "--config-file"):
        return True
    if flag in ("--find-links", "-f"):
        return _VARIABLE_VALUE.match(value) is None
    return value not in PYPI_INDEXES


def verify_installer(
    manifest: ModelReleaseCandidateManifest, script: str
) -> list[ModelReleaseCandidateFinding]:
    findings: list[ModelReleaseCandidateFinding] = []
    installer = manifest.installer
    if installer is None:
        return [
            _finding(
                K.INSTALLER_MISSING_FROM_MANIFEST,
                "installer",
                "the manifest pins no installer sha256",
            )
        ]

    actual = hashlib.sha256(script.encode("utf-8")).hexdigest()
    if actual != installer.sha256:
        findings.append(
            _finding(
                K.INSTALLER_SHA256,
                "installer",
                f"{actual}, manifest {installer.sha256}",
            )
        )

    lines = _logical_lines(script)
    packages: list[str] = []
    versions: list[str] = []
    for _n, text in lines:
        for segment in _segments(_tokens(text)):
            match = _ASSIGNMENT.match(" ".join(segment))
            if match:
                name, value = match.group(1), match.group(2)
                if name == "ONEX_PACKAGE":
                    packages.append(value)
                else:
                    versions.append(value)

    if len(packages) != 1 or (
        packages and _normalize(packages[0]) != _normalize(installer.package)
    ):
        findings.append(
            _finding(
                K.INSTALLER_PACKAGE,
                "ONEX_PACKAGE",
                f"{', '.join(packages) or 'none'}, manifest {installer.package}",
            )
        )

    distribution = None
    for dist in manifest.distributions:
        if _normalize(dist.name) == _normalize(installer.package):
            distribution = dist
            break
    if distribution is None:
        findings.append(
            _finding(
                K.INSTALLER_PACKAGE,
                installer.package,
                "the manifest pins an installer package it does not list",
            )
        )
    elif len(versions) != 1 or versions[0] != distribution.version:
        findings.append(
            _finding(
                K.INSTALLER_VERSION,
                "ONEX_VERSION",
                f"{', '.join(versions) or 'none'}, manifest {distribution.version}",
            )
        )

    for n, text in lines:
        for flag, value in _index_flags(_tokens(text)):
            if _refused_source(flag, value):
                findings.append(
                    _finding(K.INSTALLER_INDEX, f"line {n}", f"{flag} {value}: {text}")
                )

        for match in _INDEX_VARIABLE.finditer(text):
            name, value = match.group(1), match.group(2)
            clean = value.strip("\"'")
            allowed = (
                name
                in {
                    "UV_INDEX_URL",
                    "UV_DEFAULT_INDEX",
                    "UV_INDEX",
                    "PIP_INDEX_URL",
                }
                and clean in PYPI_INDEXES
            )
            if not allowed:
                findings.append(
                    _finding(
                        K.INSTALLER_INDEX,
                        f"line {n}",
                        f"{name}={value}: {text}",
                    )
                )

        for match in _BUILD_VARIABLE.finditer(text):
            findings.append(
                _finding(
                    K.INSTALLER_BUILD,
                    f"line {n}",
                    f"{match.group(1)}={match.group(2)}: {text}",
                )
            )

        for segment in _segments(_tokens(text)):
            if _is_install(segment) and not any(
                token in _NO_BUILD_TOKENS for token in segment
            ):
                findings.append(_finding(K.INSTALLER_BUILD, f"line {n}", text))

    return findings
