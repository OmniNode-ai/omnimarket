# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Tests for the one-producer-per-contract guard (OMN-14285 / WI-1, S1d).

Verifies the structural ratchet that keeps OCC companion authoring converged on
the single producer: the live converged repo passes, the template-marker
predicate is precise, and a resurrected second writer surface is rejected.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = (
    Path(__file__).resolve().parents[2] / "scripts" / "ci" / "check_one_occ_producer.py"
)


def _load_module() -> object:
    spec = importlib.util.spec_from_file_location("check_one_occ_producer", _SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_OCC_TEMPLATE = (
    'schema_version: "1.0.0"\n'
    'ticket_id: "OMN-1"\n'
    'contract_sha256: "sha256:PENDING"\n'
    "dod_evidence:\n  - id: x\n"
)


@pytest.mark.unit
class TestTemplateMarkerPredicate:
    def test_flags_a_real_companion_template(self) -> None:
        mod = _load_module()
        assert mod._is_occ_template_literal(_OCC_TEMPLATE) is True

    def test_does_not_flag_a_reader_context_string(self) -> None:
        mod = _load_module()
        # A message that mentions the field names but is not an authoring template.
        reader = (
            "receipt contract_sha256 mismatch; rerun probes or mint a per-entry hash"
        )
        assert mod._is_occ_template_literal(reader) is False

    def test_requires_all_marker_groups(self) -> None:
        mod = _load_module()
        # schema_version + contract_sha256 but no dod_evidence/evidence_item_id.
        partial = 'schema_version: "1.0.0"\ncontract_sha256: "sha256:x"\n'
        assert mod._is_occ_template_literal(partial) is False


@pytest.mark.unit
class TestLiveRepoIsConverged:
    def test_live_repo_has_exactly_one_producer(self) -> None:
        """The converged repo passes the guard with zero violations."""
        mod = _load_module()
        violations = mod.find_violations()
        assert violations == [], (
            f"guard found violations in converged repo: {violations}"
        )

    def test_main_exits_zero_on_converged_repo(self) -> None:
        mod = _load_module()
        assert mod.main([]) == 0
