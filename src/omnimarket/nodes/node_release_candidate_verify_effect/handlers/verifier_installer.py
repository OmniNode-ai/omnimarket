# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The served curl installer against the candidate manifest (OMN-25070 AC4).

The script must be the one the manifest pins by sha256, set the manifest's customer package and its
version exactly once, name no package index or package source other than PyPI, change no build
setting through the environment, and run every install or run command with the exact --no-build
flag. A mismatch is a finding that names the line; nothing raises.
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
    r"(ONEX_PACKAGE|ONEX_VERSION)=(\S*)"
)
_INDEX_FLAGS = frozenset(
    {
        "-i",
        "--index-url",
        "--extra-index-url",
        "--default-index",
        "--index",
        "--find-links",
        "-f",
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
_NO_BUILD_TOKENS = frozenset({"--no-build", "--only-binary=:all:"})
_SEPARATORS = frozenset({";", "&&", "||", "|", "&", "(", ")", "{", "}"})
_INSTALL_TOOL = re.compile(r"pip[0-9.]*|pipx|tool|uv")
_UV_SUBCOMMANDS = frozenset({"add", "sync", "run"})


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _finding(kind: K, subject: str, detail: str = "") -> ModelReleaseCandidateFinding:
    return ModelReleaseCandidateFinding(kind=kind, subject=subject, detail=detail)


def _logical_lines(script: str) -> list[tuple[int, str]]:
    lines = script.splitlines()
    logical: list[tuple[int, str]] = []
    i = 0
    while i < len(lines):
        start = i
        text = lines[i]
        while text.endswith("\\") and i + 1 < len(lines):
            text = text[:-1] + " " + lines[i + 1]
            i += 1
        i += 1
        stripped = text.strip()
        if not stripped or stripped.startswith("#"):
            continue
        logical.append((start + 1, stripped))
    return logical


def _tokens(line: str) -> list[str]:
    try:
        lex = shlex.shlex(line, posix=True, punctuation_chars=";&|(){}")
        lex.whitespace_split = True
        lex.commenters = ""
        return list(lex)
    except ValueError:
        return line.split()


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
    """Every index or package-source flag with its value, as `flag value` or `flag=value`."""
    pairs: list[tuple[str, str]] = []
    for segment in _segments(tokens):
        i = 0
        while i < len(segment):
            token = segment[i]
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
    """--index-strategy and --config-file are always refused; find-links only from a variable."""
    if flag in ("--index-strategy", "--config-file"):
        return True
    if flag in ("--find-links", "-f"):
        return not value.startswith("$")
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

    if len(set(packages)) != 1 or (
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
    elif len(set(versions)) != 1 or versions[0] != distribution.version:
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
