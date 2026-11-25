# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The store check of rule 7: no store address, port, database, key prefix, host name or implementation kind in source or contracts.

It reads the settings-store surface of omnimarket (the files that wire, hold, read or hand on the store, and the contracts
of the nodes that use it) and reports each line that writes one of those values as a literal. A name taken from the wiring
(``bootstrap[PORT_INPUT]``, ``${FROM_WIRING}``) is not a finding. There is no allowlist.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import NamedTuple

STORE_SURFACE: tuple[str, ...] = (
    "handlers/floor_fill_scheduled_tick.py",
    "handlers/overlay_store/reader_store.py",
    "handlers/overlay_store/store_wiring.py",
    "handlers/overlay_store/store_backend_registry.py",
    "handlers/overlay_store/store_valkey.py",
    "handlers/overlay_store/backend_in_memory.py",
    "handlers/overlay_store/backend_postgres.py",
    "handlers/overlay_store/backend_valkey.py",
    "handlers/overlay_store/valkey_acl.py",
    "nodes/node_floor_fill_scheduled_effect/handlers/handler_floor_fill_scheduled.py",
    "nodes/node_floor_fill_scheduled_effect/contract.yaml",
    "nodes/node_host_table_keys_ingest_effect/contract.yaml",
)

_KINDS = r"(?:store_in_memory|store_postgres|store_valkey|in_memory|postgres|valkey|bus_snapshot)"
_IDENT = r"[A-Za-z0-9_.-]"
_KNOWN_SUFFIXES = frozenset({".py", ".yaml", ".yml"})


class Finding(NamedTuple):
    path: str
    line: int
    kind: str
    text: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.kind}: {self.text.strip()}"


class _Rule(NamedTuple):
    kind: str
    pattern: re.Pattern[str]
    languages: frozenset[str]  # extensions the rule applies to; empty means every file
    skip_in_backend_modules: bool = False


_RULES: tuple[_Rule, ...] = (
    _Rule("address", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), frozenset()),
    _Rule(
        "address",
        re.compile(r"\b(?:redis|rediss|valkey|postgres|postgresql|kafka|https?)://\S+"),
        frozenset(),
    ),
    _Rule("address", re.compile(rf"\b{_IDENT}+\.{_IDENT}+:\d{{2,5}}\b"), frozenset()),
    _Rule("address", re.compile(r"[\"']localhost[\"']|\blocalhost:\d+"), frozenset()),
    _Rule("port", re.compile(r"\bports?\s*[=:]\s*\d+"), frozenset()),
    _Rule("database", re.compile(r"\b(?:database|db)\s*[=:]\s*\d+"), frozenset()),
    _Rule(
        "key-prefix",
        re.compile(r"\bkey_prefix\s*=\s*[\"'][^\"']+[\"']"),
        frozenset({".py", ""}),
    ),
    _Rule(
        "key-prefix",
        re.compile(r"^\s*key_prefix\s*:\s*[A-Za-z0-9_.-]+(?::[A-Za-z0-9_.-]+)*\s*$"),
        frozenset({".yaml", ".yml", ""}),
    ),
    _Rule(
        "implementation-kind",
        re.compile(rf"[\"']{_KINDS}[\"']"),
        frozenset({".py", ""}),
        skip_in_backend_modules=True,
    ),
    _Rule(
        "implementation-kind",
        re.compile(r"\b(?:kind|backend|implementation)\s*=\s*[\"'][a-z_]+[\"']"),
        frozenset({".py", ""}),
        skip_in_backend_modules=True,
    ),
    _Rule(
        "implementation-kind",
        re.compile(rf"^\s*(?:kind|backend|implementation)\s*:\s*{_KINDS}\s*$"),
        frozenset({".yaml", ".yml", ""}),
        skip_in_backend_modules=True,
    ),
)


def scan_text(path: str, text: str) -> list[Finding]:
    """Every line of ``text`` that writes a store value as a literal."""
    suffix = Path(path).suffix
    backend_module = Path(path).name.startswith("backend_")
    findings: list[Finding] = []
    for number, line in enumerate(text.splitlines(), start=1):
        seen: set[str] = set()
        for rule in _RULES:
            if (
                suffix in _KNOWN_SUFFIXES
                and rule.languages
                and suffix not in rule.languages
            ):
                continue
            if rule.skip_in_backend_modules and backend_module:
                continue
            if rule.kind not in seen and rule.pattern.search(line):
                seen.add(rule.kind)
                findings.append(Finding(path, number, rule.kind, line))
    return findings


def find_store_findings(
    package_root: Path, *, surface: Sequence[str] = STORE_SURFACE
) -> list[Finding]:
    """The findings of every surface file under ``package_root``; a surface file that is missing is itself a finding."""
    findings: list[Finding] = []
    for relative in surface:
        target = package_root / relative
        if not target.is_file():
            findings.append(Finding(relative, 0, "surface-file-missing", ""))
            continue
        findings.extend(scan_text(relative, target.read_text(encoding="utf-8")))
    return findings


def main(argv: Iterable[str] | None = None) -> int:
    """``python -m omnimarket.handlers.overlay_store.store_address_check``: the pre-commit and CI entry; exit 1 on a finding."""
    import sys

    import omnimarket

    findings = find_store_findings(Path(omnimarket.__file__).parent)
    for finding in findings:
        sys.stderr.write(f"{finding}\n")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())

__all__ = ["STORE_SURFACE", "Finding", "find_store_findings", "main", "scan_text"]
