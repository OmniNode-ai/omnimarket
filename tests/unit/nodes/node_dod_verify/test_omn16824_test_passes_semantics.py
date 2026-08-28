# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-16824 -- the local half of the cross-runner ``test_passes`` semantic.

``check_type: test_passes`` used to mean two different things:

* here, in ``node_dod_verify``, it EXECUTED ``check_value`` and honoured the
  check's ``cwd``;
* in the hosted ``Contract Compliance Check`` (onex_change_control) it IGNORED
  ``check_value`` and reported whether the PR's own CI was green.

OMN-16824 settled it on this runner's reading and fixed the hosted one. The
case table below is checked into BOTH repos byte-for-byte and executed against
BOTH runners -- this module runs it against ``EvidenceCollector``, and
``onex_change_control/tests/test_omn16824_test_passes_semantics.py`` runs the
identical cases against the hosted gate. If either runner ever answers a
different question again, its half goes red.

The pinned digest is the mechanical link: neither copy can be edited without
failing its own repo's test, which forces the edit to be deliberate in both.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_dod_verify.models.model_dod_verify_state import (
    EnumEvidenceCheckStatus,
)
from omnimarket.nodes.node_dod_verify.services.evidence_collector import (
    EvidenceCollector,
)

_CASE_TABLE = (
    Path(__file__).parents[3] / "fixtures" / "check_type_runner_semantics.yaml"
)

# Identical to onex_change_control's CASE_TABLE_DIGEST over its identical copy.
# Taken over the PARSED table in canonical JSON, not the file bytes: each repo's
# yamlfmt reflows YAML on commit, and a reflow is not a change of meaning.
CASE_TABLE_DIGEST = "338cc633d858d71e19e1fc6b2ac76a54c9596e490f1c6a2fd8211b9e845e79c4"


def _case_table_digest(table: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(table, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _table() -> dict[str, Any]:
    loaded = yaml.safe_load(_CASE_TABLE.read_text())
    assert isinstance(loaded, dict)
    return loaded


def _cases() -> list[dict[str, Any]]:
    return list(_table()["cases"])


def _write_contract(root: Path, check: dict[str, Any]) -> Path:
    contract = {
        "schema_version": "1.0.0",
        "ticket_id": "OMN-16824",
        "dod_evidence": [
            {
                "id": "dod-16824-case",
                "description": "One shared cross-runner semantics case.",
                "checks": [check],
            }
        ],
    }
    path = root / "OMN-16824.yaml"
    path.write_text(yaml.dump(contract), encoding="utf-8")
    return path


@pytest.mark.unit
def test_case_table_content_is_pinned() -> None:
    """The shared table cannot drift in one repo without failing that repo."""
    assert _case_table_digest(_table()) == CASE_TABLE_DIGEST, (
        "tests/fixtures/check_type_runner_semantics.yaml changed. It is shared "
        "byte-for-byte with onex_change_control: update BOTH copies and BOTH "
        "pinned digests, or the two runners can diverge again silently."
    )


@pytest.mark.unit
@pytest.mark.parametrize("case", _cases(), ids=lambda c: str(c["id"]))
def test_local_runner_matches_the_shared_semantic(
    case: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    for rel in _table()["fixture_files"]:
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("fixture\n")
    monkeypatch.setenv("OMNI_HOME", str(root))

    check: dict[str, Any] = {
        "check_type": case["check_type"],
        "check_value": case["check_value"],
    }
    if case.get("cwd"):
        check["cwd"] = case["cwd"]

    contract_path = _write_contract(root, check)
    results = EvidenceCollector().collect("OMN-16824", contract_path=str(contract_path))

    assert len(results) == 1
    observed = (
        "verified"
        if results[0].status == EnumEvidenceCheckStatus.VERIFIED
        else "refused"
    )
    assert observed == case["expected"], (
        f"case {case['id']}: expected {case['expected']}, got "
        f"{results[0].status} -- {results[0].message}\n{case['reason']}"
    )


@pytest.mark.unit
def test_test_passes_and_command_are_the_same_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The alias must not be able to acquire its own meaning again.

    This is the property the hosted runner lost: ``test_passes`` there stopped
    executing ``check_value`` at all, so the same entry meant a behaviour proof
    to this runner and a PR-status proxy to that one.
    """
    root = tmp_path / "root"
    root.mkdir()
    (root / "marker.txt").write_text("x\n")
    monkeypatch.setenv("OMNI_HOME", str(root))

    for check_type in ("command", "test_passes"):
        for check_value, expected in (
            ("test -f marker.txt", EnumEvidenceCheckStatus.VERIFIED),
            ("test -f absent.txt", EnumEvidenceCheckStatus.FAILED),
        ):
            contract_path = _write_contract(
                root,
                {
                    "check_type": check_type,
                    "check_value": check_value,
                    "cwd": "${OMNI_HOME}",
                },
            )
            results = EvidenceCollector().collect(
                "OMN-16824", contract_path=str(contract_path)
            )
            assert results[0].status == expected, (
                f"{check_type} / {check_value}: {results[0].message}"
            )
