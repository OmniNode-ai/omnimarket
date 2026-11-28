# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A contract evidence command spells every value-taking uv flag as one word.

``tests/ci/test_repo_evidence_caller_workflow.py`` reads a ``uv run`` command by skipping
uv's own flags one token at a time. A flag written as two words (``--python 3.12``) leaves
its value behind, so the reader takes the value for the command and rejects the evidence
item. This scans every ``contracts/*.yaml`` ``check_value`` and names each file and item id
that writes such a flag as two words before the command it runs (``--python=3.12`` is the
accepted spelling).
"""

from __future__ import annotations

import shlex
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CONTRACTS = _REPO_ROOT / "contracts"

# The options of `uv run --help` that take a value (read from that help, 2026-11-24).
_VALUE_FLAGS = frozenset(
    {
        "--extra", "--no-extra", "--group", "--no-group", "--only-group",
        "--no-editable-package", "--env-file", "-w", "--with", "--with-editable",
        "--with-requirements", "--package", "--python-platform", "--index",
        "--default-index", "-i", "--index-url", "--extra-index-url", "-f",
        "--find-links", "--index-strategy", "--keyring-provider", "-P",
        "--upgrade-package", "--upgrade-group", "--resolution", "--prerelease",
        "--prerelease-package", "--fork-strategy", "--exclude-newer",
        "--exclude-newer-package", "--no-sources-package", "--reinstall-package",
        "--link-mode", "-C", "--config-setting", "--config-settings-package",
        "--no-build-isolation-package", "--no-build-package", "--no-binary-package",
        "--cache-dir", "--refresh-package", "-p", "--python", "--color",
        "--allow-insecure-host", "--directory", "--project", "--config-file",
    }
)  # fmt: skip


def two_word_flags(check_value: str) -> list[str]:
    """The value-taking uv flags written as two words in a `uv run` that runs pytest.

    Reads the command the way uv does (a value-taking flag consumes the next word) and
    reports the flags only when the command it reaches is `pytest`: that is the run the
    evidence reader mistakes for another command. Other commands are not read by it.
    """
    try:
        tokens = shlex.split(check_value)
    except ValueError:
        return []
    found: list[str] = []
    for index in range(len(tokens) - 1):
        if tokens[index : index + 2] != ["uv", "run"]:
            continue
        flags: list[str] = []
        position = index + 2
        while position < len(tokens) and tokens[position].startswith("-"):
            if tokens[position] in _VALUE_FLAGS:
                flags.append(tokens[position])
                position += 1
            position += 1
        if tokens[position : position + 1] == ["pytest"]:
            found.extend(flags)
    return found


def _check_values(node: Any) -> Iterator[tuple[str, str]]:
    """Every (item id, check_value) of a `test_passes` check under a parsed contract."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "checks" and isinstance(value, list):
                for check in value:
                    if (
                        isinstance(check, dict)
                        and check.get("check_type") == "test_passes"
                        and isinstance(check.get("check_value"), str)
                    ):
                        yield str(node.get("id", "?")), check["check_value"]
            else:
                yield from _check_values(value)
    elif isinstance(node, list):
        for item in node:
            yield from _check_values(item)


def test_scanner_flags_a_planted_two_word_flag_and_accepts_the_one_word_spelling() -> (
    None
):
    """Positive control: the scan can fail, so a clean tree is evidence."""
    assert two_word_flags("uv run --frozen --python 3.12 pytest tests/x.py") == [
        "--python"
    ]
    assert two_word_flags("uv run --frozen --python=3.12 pytest tests/x.py") == []
    assert two_word_flags("uv run --with pytest-x pytest tests/x.py") == ["--with"]
    assert two_word_flags("uv run pytest tests/x.py -n 0") == []


def test_no_contract_writes_a_value_taking_uv_flag_as_two_words() -> None:
    contracts = sorted(_CONTRACTS.glob("*.yaml"))
    assert contracts, "no contracts found: the scan would be vacuous"
    offenders = []
    for path in contracts:
        document = yaml.safe_load(path.read_text())
        for item_id, check_value in _check_values(document):
            offenders.extend(
                f"{path.name}:{item_id}: `{flag}` and its value are two words; "
                f"write {flag}=<value>"
                for flag in two_word_flags(check_value)
            )
    assert not offenders, "\n".join(offenders)
