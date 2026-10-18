# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Replace private names with roles, reading the public-repo hygiene gate's own vocabulary.

The vocabulary is private and is never copied into this repository: it is read from the paths
the request names, at run time. The gate stores it in a restricted YAML subset (top-level scalars
and scalar lists, double-quoted values taken raw, so a regex such as ``\\s`` is not an escape), which
is parsed here the same way. A vocabulary that is missing, unparsable or empty in a class that must
not be empty is refused: a replacement list that silently holds nothing would let every name through.

Shapes that are not secret (an IPv4 address, a ``.local`` host name, a lab host label, a home
directory) are matched here by shape, like the gate does for its structural classes.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

ROLE_PRIVATE_REPO = "a private repository"
ROLE_HOST = "a lab host"
ROLE_ADDRESS = "a lab address"
ROLE_PERSON = "a teammate"
ROLE_LANE = "a lab lane"
ROLE_PATH = "a local path"
ROLE_TRACKER = "a tracker link"

_STRUCTURAL: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    ("tracker", ROLE_TRACKER, re.compile(r"https?://[^\s)>\]]+", re.IGNORECASE)),
    (
        "path",
        ROLE_PATH,
        re.compile(
            r"(?<![\w.])(?:/home|/Users|/Volumes|/scratch|/data)/[^\s|,;)\]>\"'`]+"
        ),
    ),
    (
        "address",
        ROLE_ADDRESS,
        re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?(?![\d.])"),
    ),
    (
        "host",
        ROLE_HOST,
        re.compile(
            r"(?<![\w.-])(?:[A-Za-z0-9-]+\.local|h[12]\d{2}|omnipc\d?)(?![\w-])",
            re.IGNORECASE,
        ),
    ),
)


class HygieneVocabularyError(ValueError):
    """The vocabulary could not be read or is unfit to replace names with."""


class LabStoryLogLeakError(RuntimeError):
    """A private name survived replacement; the log is not written."""


def parse_restricted_yaml(text: str, origin: str) -> dict[str, object]:
    """Parse the gate's YAML subset: top-level scalars and scalar lists, values taken raw."""
    parsed: dict[str, object] = {}
    current: list[str] | None = None
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        item = re.match(r"^\s+-\s+(.*)$", line)
        if item is not None:
            if current is None:
                raise HygieneVocabularyError(
                    f"{origin}:{number}: a list item outside a list"
                )
            current.append(_unquote(item.group(1)))
            continue
        key = re.match(r"^([A-Za-z_][A-Za-z0-9_]*):\s*(.*)$", line)
        if key is None or line[0].isspace():
            raise HygieneVocabularyError(
                f"{origin}:{number}: a line the restricted subset does not allow"
            )
        name, value = key.groups()
        if value == "":
            current = []
            parsed[name] = current
        else:
            current = None
            parsed[name] = _unquote(value)
    return parsed


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _string_list(parsed: dict[str, object], key: str, origin: str) -> tuple[str, ...]:
    value = parsed.get(key, [])
    if not isinstance(value, list):
        raise HygieneVocabularyError(f"{origin}: {key} is not a list")
    return tuple(item for item in value if item)


def _literal_pattern(values: Iterable[str]) -> re.Pattern[str] | None:
    ordered = sorted({v for v in values if v}, key=len, reverse=True)
    if not ordered:
        return None
    body = "|".join(re.escape(v) for v in ordered)
    return re.compile(rf"(?<![A-Za-z0-9_])(?:{body})(?![A-Za-z0-9_])", re.IGNORECASE)


def _regex_pattern(fragments: Iterable[str], origin: str) -> re.Pattern[str] | None:
    fragments = [f for f in fragments if f]
    if not fragments:
        return None
    try:
        return re.compile("|".join(f"(?:{f})" for f in fragments), re.IGNORECASE)
    except re.error as exc:
        raise HygieneVocabularyError(
            f"{origin}: a vocabulary regex does not compile: {exc}"
        ) from exc


@dataclass(frozen=True)
class Sanitizer:
    """Compiled replacement list: ordered (class, role, pattern)."""

    rules: tuple[tuple[str, str, re.Pattern[str]], ...]
    counts: Counter[str] = field(default_factory=Counter, compare=False)

    def sanitize(self, text: str) -> str:
        for name, role, pattern in self.rules:
            text, n = pattern.subn(role, text)
            if n:
                self.counts[name] += n
        return text

    def leaks(self, text: str) -> list[tuple[str, str]]:
        """Every (class, matched text) still in `text`; empty when nothing survives."""
        found: list[tuple[str, str]] = []
        for name, _role, pattern in self.rules:
            found.extend((name, m.group(0)) for m in pattern.finditer(text))
        return found


def load_sanitizer(
    hygiene_vocabulary: Path,
    person_names: Path | None = None,
    lab_vocabulary: Path | None = None,
) -> Sanitizer:
    """Build the replacement list from the gate's vocabulary files; refuse one that holds nothing."""
    try:
        parsed = parse_restricted_yaml(
            hygiene_vocabulary.read_text(encoding="utf-8"), str(hygiene_vocabulary)
        )
    except OSError as exc:
        raise HygieneVocabularyError(
            f"cannot read the hygiene vocabulary: {exc}"
        ) from exc
    origin = "hygiene vocabulary"
    repos = _string_list(parsed, "private_repo_names", origin)
    if not repos:
        raise HygieneVocabularyError(
            f"{origin}: private_repo_names is empty; refusing to write a log"
        )
    names = list(_string_list(parsed, "person_names", origin))
    if person_names is not None:
        try:
            lines = person_names.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise HygieneVocabularyError(
                f"cannot read the person names list: {exc}"
            ) from exc
        names.extend(
            s for s in (ln.strip() for ln in lines) if s and not s.startswith("#")
        )
    if not names:
        raise HygieneVocabularyError(
            f"{origin}: no person names; refusing to write a log"
        )
    lanes: tuple[str, ...] = ()
    if lab_vocabulary is not None:
        try:
            lab = parse_restricted_yaml(
                lab_vocabulary.read_text(encoding="utf-8"), str(lab_vocabulary)
            )
        except OSError as exc:
            raise HygieneVocabularyError(
                f"cannot read the lab vocabulary: {exc}"
            ) from exc
        lanes = _string_list(lab, "lab_lane_ids", "lab vocabulary")
    host_regex = _regex_pattern(
        [*_string_list(parsed, "host_nicknames", origin)], origin
    )
    path_regex = _regex_pattern(
        _string_list(parsed, "machine_path_patterns", origin), origin
    )
    tracker_regex = _regex_pattern(
        _string_list(parsed, "tracker_url_patterns", origin), origin
    )
    rules: list[tuple[str, str, re.Pattern[str] | None]] = [
        ("tracker", ROLE_TRACKER, tracker_regex),
        *((n, r, p) for n, r, p in _STRUCTURAL[:2]),
        ("path", ROLE_PATH, path_regex),
        ("address", ROLE_ADDRESS, _STRUCTURAL[2][2]),
        ("host", ROLE_HOST, host_regex),
        ("host", ROLE_HOST, _STRUCTURAL[3][2]),
        ("private_repo", ROLE_PRIVATE_REPO, _literal_pattern(repos)),
        ("lane", ROLE_LANE, _literal_pattern(lanes)),
        ("person", ROLE_PERSON, _literal_pattern(names)),
    ]
    return Sanitizer(tuple((n, r, p) for n, r, p in rules if p is not None))


__all__ = [
    "HygieneVocabularyError",
    "LabStoryLogLeakError",
    "Sanitizer",
    "load_sanitizer",
    "parse_restricted_yaml",
]
