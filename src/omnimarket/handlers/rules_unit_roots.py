# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The paths an installed unit runs from, read out of systemd unit text (OMN-22735, OMN-22736).

A unit installed from a worktree dies when the worktree is removed: its interpreter, working
directory or ledger commands are gone and every tick fails. Three units died that way on
2026-11-06. Two readers of the same text close the class:

* the installers call :func:`refuse_worktree_roots` (systemd text) or :func:`refuse_worktree_plists`
  (launchd agents) before they write a unit, and
* the worktree reconcile node asks :meth:`UnitRoots.naming` before it removes a tree.

Everything here is pure: it reads text it is handed and does no I/O.
"""

from __future__ import annotations

import plistlib
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import PurePosixPath

#: The directory name every lane worktree sits under.
WORKTREE_SEGMENT = "omni_worktrees"

#: Unit settings whose value names a path the unit runs from or reads at run time.
PATH_SETTINGS: frozenset[str] = frozenset(
    {
        "ExecStart",
        "ExecStartPre",
        "ExecStartPost",
        "ExecStop",
        "ExecStopPost",
        "ExecReload",
        "ExecCondition",
        "WorkingDirectory",
        "RootDirectory",
        "Environment",
        "EnvironmentFile",
    }
)

#: Launchd agent keys whose value names a path the agent runs from or reads at run time. The log
#: paths (``StandardOutPath``, ``StandardErrorPath``) are not among them: a log is written, not run.
PLIST_PATH_KEYS: frozenset[str] = frozenset(
    {
        "Program",
        "ProgramArguments",
        "WorkingDirectory",
        "RootDirectory",
        "EnvironmentVariables",
    }
)

# An absolute path inside any text: starts at a slash that is not part of a word, a dot or another
# path, and runs to whitespace, a path-list colon, a quote or an assignment.
_ABSOLUTE_PATH = re.compile(r"(?<![\w.~/$])/[^\s:;'\"<>=,]+")
_SETTING = re.compile(r"^\s*(?P<key>[A-Za-z]+)\s*=(?P<value>.*)$")
# `systemctl cat` starts each file with `# <absolute path>`.
_CAT_HEADER = re.compile(r"^# (?P<path>/\S+)\s*$")


class UnitRootRefusedError(ValueError):
    """A unit would run from a worktree; ``paths`` names every offending path."""

    def __init__(self, unit: str, paths: Iterable[str]) -> None:
        self.unit = unit
        self.paths = tuple(sorted(set(paths)))
        super().__init__(
            f"{unit} runs from {WORKTREE_SEGMENT}, which the worktree reconcile removes: "
            + ", ".join(self.paths)
        )


def unit_paths(text: str) -> frozenset[str]:
    """Every absolute path named by a path-bearing setting of the unit text."""
    found: set[str] = set()
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith(("#", ";")):
            continue
        match = _SETTING.match(line)
        if match is None or match.group("key") not in PATH_SETTINGS:
            continue
        value = match.group("value")
        for path in _ABSOLUTE_PATH.findall(value):
            if len(path) > 1:
                found.add(path.rstrip("/"))
    return frozenset(found)


def _plist_strings(value: object) -> Iterable[str]:
    """Every string inside a plist value: the value itself, list items, dictionary values."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _plist_strings(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _plist_strings(item)


def _command_paths(text: str) -> Iterable[str]:
    """Every absolute path inside command text or a path list."""
    for path in _ABSOLUTE_PATH.findall(text):
        if len(path) > 1:
            yield path.rstrip("/")


def _literal_path(text: str) -> Iterable[str]:
    """The whole string when it is one absolute path; otherwise the paths inside it."""
    if text.startswith("/"):
        if len(text) > 1:
            yield text.rstrip("/")
        return
    yield from _command_paths(text)


def _plist_key_paths(value: object) -> Iterable[str]:
    """The paths of one plist key: literal where the key holds a path, scanned where it holds text."""
    if isinstance(value, str):
        yield from _literal_path(value)
    elif isinstance(value, list):
        shell_command = False
        for item in value:
            if not isinstance(item, str):
                continue
            if shell_command:
                yield from _command_paths(item)
            else:
                yield from _literal_path(item)
            shell_command = item.startswith("-") and item.endswith("c")
    elif isinstance(value, dict):
        for item in value.values():
            for text in _plist_strings(item):
                for part in text.split(":"):
                    yield from _literal_path(part)


def plist_paths(data: bytes) -> frozenset[str]:
    """Every absolute path named by a path-bearing key of a launchd agent plist.

    A program or directory string is one literal path and keeps its spaces and punctuation; the
    argument after a shell ``-c`` flag is command text, and an environment value is a path list split
    at its colons.
    """
    document = plistlib.loads(data)
    found: set[str] = set()
    for key in PLIST_PATH_KEYS:
        found.update(_plist_key_paths(document.get(key)))
    return frozenset(found)


def environment_files(text: str) -> tuple[tuple[str, bool], ...]:
    """The ``EnvironmentFile=`` paths of the unit text, each with whether it may be absent."""
    files: list[tuple[str, bool]] = []
    for line in text.splitlines():
        if line.lstrip().startswith(("#", ";")):
            continue
        match = _SETTING.match(line)
        if match is None or match.group("key") != "EnvironmentFile":
            continue
        value = match.group("value").strip().strip('"')
        optional = value.startswith("-")
        path = value.lstrip("-")
        if path.startswith("/"):
            files.append((path, optional))
    return tuple(files)


def environment_file_paths(text: str) -> frozenset[str]:
    """Every absolute path named by the lines of an environment file."""
    found: set[str] = set()
    for line in text.splitlines():
        if line.lstrip().startswith(("#", ";")):
            continue
        _, _, value = line.partition("=")
        for path in _ABSOLUTE_PATH.findall(value):
            if len(path) > 1:
                found.add(path.rstrip("/"))
    return frozenset(found)


def under_worktrees(path: str) -> bool:
    """True when the path has an ``omni_worktrees`` directory segment."""
    return WORKTREE_SEGMENT in PurePosixPath(path).parts


def worktree_paths(text: str) -> tuple[str, ...]:
    """The paths of the unit text that sit under ``omni_worktrees``, sorted."""
    return tuple(sorted(path for path in unit_paths(text) if under_worktrees(path)))


def refuse_worktree_roots(units: Mapping[str, str]) -> None:
    """Raise :class:`UnitRootRefusedError` for the first unit whose text names a worktree path."""
    for name, text in units.items():
        offending = worktree_paths(text)
        if offending:
            raise UnitRootRefusedError(name, offending)


def refuse_worktree_plists(units: Mapping[str, bytes]) -> None:
    """Raise :class:`UnitRootRefusedError` for the first agent plist that names a worktree path."""
    for name, data in units.items():
        offending = sorted(path for path in plist_paths(data) if under_worktrees(path))
        if offending:
            raise UnitRootRefusedError(name, offending)


def split_systemctl_cat(output: str) -> dict[str, str]:
    """Unit name to its text (drop-ins joined), from the output of ``systemctl cat``."""
    units: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in output.splitlines():
        header = _CAT_HEADER.match(line)
        if header is not None:
            file = PurePosixPath(header.group("path"))
            name = (
                file.parent.name.removesuffix(".d")
                if file.parent.name.endswith(".d")
                else file.name
            )
            current = units.setdefault(name, [])
            continue
        if current is not None:
            current.append(line)
    return {name: "\n".join(lines) for name, lines in units.items()}


@dataclass(frozen=True)
class UnitRoots:
    """Every absolute path an installed unit names, with one unit that names it."""

    named: Mapping[str, str] = field(default_factory=dict)
    units_read: int = 0

    def naming(self, tree: str) -> str:
        """The unit that names ``tree`` or a path inside it; empty when none does."""
        root = tree.rstrip("/")
        prefix = root + "/"
        for path, unit in self.named.items():
            if path == root or path.startswith(prefix):
                return unit
        return ""

    @classmethod
    def from_paths(cls, per_unit: Mapping[str, Iterable[str]]) -> UnitRoots:
        named: dict[str, str] = {}
        for unit, paths in per_unit.items():
            for path in paths:
                named.setdefault(path, unit)
        return cls(named=named, units_read=len(per_unit))
