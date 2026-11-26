"""The OMN-26691 AC2 tag-check item runs only at the local Done gate, checks v1.1.16 by default, and starts with a program a lab host and our host allow (AC5)."""

from __future__ import annotations

import re
import shlex
from pathlib import Path, PurePosixPath

import pytest
import yaml

from omnimarket.nodes.node_lab_work_unit_effect.handlers.handler_lab_work_unit_effect import (
    DEFAULT_ALLOWED_EXECUTABLES,
)

pytestmark = pytest.mark.unit

_CONTRACT = Path(__file__).resolve().parents[2] / "contracts" / "OMN-26691.yaml"
_ITEM = "dod-omn26691-ac2-tag-check"


def _command_words() -> list[str]:
    document = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    entries = [e for e in document["dod_evidence"] if e["id"] == _ITEM]
    assert len(entries) == 1, f"{_ITEM} is not exactly one entry"
    checks = entries[0]["checks"]
    assert len(checks) == 1, f"{_ITEM} does not have exactly one check"
    return shlex.split(checks[0]["check_value"])


def test_the_item_exists_exactly_once() -> None:
    document = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    entries = [e for e in document["dod_evidence"] if e["id"] == _ITEM]
    assert len(entries) == 1, f"{_ITEM} is not exactly one entry"
    checks = entries[0]["checks"]
    assert len(checks) == 1, f"{_ITEM} does not have exactly one check"


def test_the_item_runs_only_at_the_local_done_gate() -> None:
    document = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    entries = [e for e in document["dod_evidence"] if e["id"] == _ITEM]
    assert len(entries) == 1, f"{_ITEM} is not exactly one entry"
    assert entries[0]["execution_scope"] == "local_done_gate"


def test_the_item_binds_no_criterion() -> None:
    document = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    entries = [e for e in document["dod_evidence"] if e["id"] == _ITEM]
    assert len(entries) == 1, f"{_ITEM} is not exactly one entry"
    assert not entries[0].get("binds_ac")


def test_the_item_defaults_the_tag_to_v1_1_16() -> None:
    document = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    entries = [e for e in document["dod_evidence"] if e["id"] == _ITEM]
    assert len(entries) == 1, f"{_ITEM} is not exactly one entry"
    checks = entries[0]["checks"]
    assert len(checks) == 1, f"{_ITEM} does not have exactly one check"
    check_value = checks[0]["check_value"]
    match = re.search(r'os\.environ\.get\("TAG",\s*"([^"]+)"\)', check_value)
    assert match is not None
    assert match.group(1) == "v1.1.16"


def test_the_item_starts_with_an_allowed_program() -> None:
    words = _command_words()
    assert PurePosixPath(words[0]).name in DEFAULT_ALLOWED_EXECUTABLES
