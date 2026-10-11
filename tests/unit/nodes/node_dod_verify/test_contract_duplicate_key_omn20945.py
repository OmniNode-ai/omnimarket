# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-20945 — a contract YAML with a duplicate mapping key is refused.

``yaml.safe_load`` keeps the last of two equal keys, so an evidence item whose
``description`` and ``command`` were overwritten by a later duplicate parsed
clean and could read as a pass or as "no evidence required". The collector now
loads contracts through the strict omnibase_core loader and surfaces the
duplicate as a FAILED result naming the file, the line and the key.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from omnibase_core.errors.error_duplicate_yaml_mapping_key import (
    DuplicateYamlMappingKeyError,
)
from omnibase_core.utils.util_safe_yaml_loader import load_yaml_mapping_no_duplicates

from omnimarket.nodes.node_dod_sweep_orchestrator.handlers.handler_dod_sweep_orchestrator import (
    _check_receipt_exists,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_state import (
    EnumEvidenceCheckStatus,
)
from omnimarket.nodes.node_dod_verify.services.evidence_collector import (
    EvidenceCollector,
)

_CLEAN = """\
schema_version: "1.0.0"
ticket_id: OMN-20945
evidence_requirements:
  - kind: tests
    description: first item
    command: "true"
dod_evidence:
  - id: dod-001
    description: first item
    checks:
      - check_type: command
        check_value: "true"
"""

# The evidence_requirements item carries ``command`` twice (line 7). safe_load
# keeps the later value and the item loses the first without a word.
_DUPLICATED = """\
schema_version: "1.0.0"
ticket_id: OMN-20945
evidence_requirements:
  - kind: tests
    description: first item
    command: "true"
    command: "false"
dod_evidence:
  - id: dod-001
    description: first item
    checks:
      - check_type: command
        check_value: "true"
"""


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "OMN-20945.yaml"
    path.write_text(text, encoding="utf-8")
    return path


@pytest.mark.unit
class TestContractDuplicateKey:
    def test_duplicate_evidence_item_key_is_a_typed_failure(
        self, tmp_path: Path
    ) -> None:
        path = _write(tmp_path, _DUPLICATED)
        results = EvidenceCollector().collect("OMN-20945", contract_path=str(path))

        assert len(results) == 1
        result = results[0]
        assert result.evidence_id == "contract"
        assert result.status == EnumEvidenceCheckStatus.FAILED
        assert str(path) in result.message
        assert ":7:" in result.message
        assert "'command'" in result.message
        assert "duplicate key" in result.message

    def test_clean_contract_is_unchanged(self, tmp_path: Path) -> None:
        path = _write(tmp_path, _CLEAN)
        results = EvidenceCollector().collect("OMN-20945", contract_path=str(path))

        assert [r.evidence_id for r in results] == ["dod-001"]
        assert results[0].status == EnumEvidenceCheckStatus.VERIFIED

    def test_sweep_receipt_exists_refuses_duplicate_key(self, tmp_path: Path) -> None:
        clean = _check_receipt_exists("OMN-20945", _write(tmp_path, _CLEAN))
        assert clean.status == "pass"

        result = _check_receipt_exists("OMN-20945", _write(tmp_path, _DUPLICATED))
        assert result.status == "fail"
        assert result.details["reason"] == "yaml_parse_error"
        assert ":7:" in result.details["error"]
        assert "'command'" in result.details["error"]


@pytest.mark.unit
def test_scan_positive_control_names_the_planted_duplicate(tmp_path: Path) -> None:
    """The AC3 scan loop names a planted duplicate and passes a clean sibling."""
    (tmp_path / "OMN-1.yaml").write_text(_CLEAN, encoding="utf-8")
    (tmp_path / "OMN-2.yaml").write_text(_DUPLICATED, encoding="utf-8")

    found = []
    for contract in sorted(tmp_path.glob("*.yaml")):
        try:
            load_yaml_mapping_no_duplicates(
                contract.read_text(encoding="utf-8"), source=contract.name
            )
        except DuplicateYamlMappingKeyError as error:
            found.append((error.source, error.line, error.key))

    assert found == [("OMN-2.yaml", 7, "command")]
