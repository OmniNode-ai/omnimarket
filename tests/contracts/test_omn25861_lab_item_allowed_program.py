"""The OMN-25861 integration-proof item starts with programs a lab host runs.

A lab host refuses a work unit whose first word is not in its allowlist
(``DEFAULT_ALLOWED_EXECUTABLES``). The item's command used to start with ``env``,
and the lab refused it (work unit lw-c7881ca86a554918, 2026-11-25). The command
the caller runs and the command it sends after ``--`` must each start with an
allowed program.
"""

from __future__ import annotations

import shlex
from pathlib import Path, PurePosixPath

import pytest
import yaml

from omnimarket.nodes.node_lab_work_unit_effect.handlers.handler_lab_work_unit_effect import (
    DEFAULT_ALLOWED_EXECUTABLES,
)

pytestmark = pytest.mark.unit

_CONTRACT = Path(__file__).resolve().parents[2] / "contracts" / "OMN-25861.yaml"
_ITEM = "dod-omn25861-integration-proof"


def _command_words() -> list[str]:
    document = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    entries = [e for e in document["dod_evidence"] if e["id"] == _ITEM]
    assert len(entries) == 1, f"{_ITEM} is not exactly one entry"
    checks = entries[0]["checks"]
    assert len(checks) == 1, f"{_ITEM} does not have exactly one check"
    return shlex.split(checks[0]["check_value"])


def _program(word: str) -> str:
    return PurePosixPath(word).name


def test_the_caller_command_starts_with_an_allowed_program() -> None:
    words = _command_words()
    assert _program(words[0]) in DEFAULT_ALLOWED_EXECUTABLES, words[0]


def test_the_command_sent_to_the_lab_starts_with_an_allowed_program() -> None:
    words = _command_words()
    assert "--" in words, "the item sends no command after --"
    sent = words[words.index("--") + 1 :]
    assert sent, "the command after -- is empty"
    assert _program(sent[0]) in DEFAULT_ALLOWED_EXECUTABLES, sent[0]
