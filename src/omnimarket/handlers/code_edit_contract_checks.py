# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declared checks from a ticket's contract file (OMN-23930).

The ticket's contract names the checks that prove the ticket's done. For every
item of ``dod_evidence`` whose ``execution_scope`` is not ``local_done_gate``,
every entry of its ``checks`` list with ``check_type == "test_passes"`` becomes
a declared check. A contract with no such check raises ``ValueError``.
"""

from __future__ import annotations

import shlex
from collections.abc import Mapping
from pathlib import Path

import yaml

from omnimarket.models.model_delegated_code_edit import ModelDeclaredCheck


def declared_checks_from_contract(
    contract_path: Path, timeout_seconds: int
) -> tuple[ModelDeclaredCheck, ...]:
    """The ticket's declared checks from its contract file.

    For every item of ``dod_evidence`` whose ``execution_scope`` is not
    ``local_done_gate``, every entry of its ``checks`` list with
    ``check_type == "test_passes"`` becomes a declared check named
    ``ticket<i>`` (``i`` counting from 0 in file order), with the check's
    ``check_value`` split with ``shlex`` as its argv and the words of the argv
    that start with ``tests/`` as its targets. A contract with no such check
    raises ``ValueError``; an unreadable or non-mapping file raises
    ``ValueError``.
    """
    try:
        raw = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ValueError(f"unreadable contract {contract_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"contract {contract_path} must be a mapping")
    checks: list[ModelDeclaredCheck] = []
    index = 0
    for item in raw.get("dod_evidence", []):
        if not isinstance(item, Mapping):
            continue
        if item.get("execution_scope") == "local_done_gate":
            continue
        for check in item.get("checks", []):
            if not isinstance(check, Mapping):
                continue
            if check.get("check_type") != "test_passes":
                continue
            value = check.get("check_value", "")
            if not isinstance(value, str) or not value.strip():
                continue
            argv = tuple(shlex.split(value))
            targets = tuple(word for word in argv if word.startswith("tests/"))
            checks.append(
                ModelDeclaredCheck(
                    name=f"ticket{index}",
                    argv=argv,
                    timeout_seconds=timeout_seconds,
                    targets=targets,
                )
            )
            index += 1
    if not checks:
        raise ValueError("the contract declares no test_passes check")
    return tuple(checks)
