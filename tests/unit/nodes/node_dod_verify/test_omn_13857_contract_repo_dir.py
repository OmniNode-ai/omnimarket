# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-13857: deterministic ``$CONTRACT_REPO_DIR`` resolution for dod_verify.

Receipt-backed contract checks embed ``$CONTRACT_REPO_DIR`` (e.g.
``grep -q '^status: PASS$' "$CONTRACT_REPO_DIR/drift/dod_receipts/<T>/.../command.yaml"``).
Before this fix the token was satisfied only when the *caller* exported
``CONTRACT_REPO_DIR``; unset, it expanded to the empty string and every
receipt check FAILED with a missing-prefix path — a false-negative verdict on
genuinely-passing evidence. These tests prove the collector now resolves the
OCC root deterministically and injects it into the check subprocess env.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_dod_verify.models.model_dod_verify_state import (
    EnumEvidenceCheckStatus,
)
from omnimarket.nodes.node_dod_verify.services.evidence_collector import (
    EvidenceCollector,
)


def _clear_repo_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("CONTRACT_REPO_DIR", "OMNI_HOME", "ONEX_CC_REPO_PATH"):
        monkeypatch.delenv(var, raising=False)


def _make_occ_tree(root: Path, ticket_id: str, status: str = "PASS") -> Path:
    """Create <root>/onex_change_control with a contract + one receipt.

    Returns the contract path under ``.../onex_change_control/contracts/``.
    """
    occ = root / "onex_change_control"
    receipt = occ / "drift" / "dod_receipts" / ticket_id / "item-a" / "command.yaml"
    receipt.parent.mkdir(parents=True)
    receipt.write_text(f"status: {status}\n", encoding="utf-8")

    contracts_dir = occ / "contracts"
    contracts_dir.mkdir(parents=True)
    contract = {
        "schema_version": "1.0.0",
        "ticket_id": ticket_id,
        "dod_evidence": [
            {
                "id": "dod-receipt",
                "description": "receipt is PASS",
                "checks": [
                    {
                        "check_type": "command",
                        "check_value": (
                            "grep -q '^status: PASS$' "
                            f'"$CONTRACT_REPO_DIR/drift/dod_receipts/{ticket_id}/'
                            'item-a/command.yaml"'
                        ),
                    }
                ],
            }
        ],
    }
    contract_path = contracts_dir / f"{ticket_id}.yaml"
    contract_path.write_text(yaml.dump(contract), encoding="utf-8")
    return contract_path


# ---------------------------------------------------------------------------
# _resolve_contract_repo_dir — pure resolution order
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestResolveContractRepoDir:
    def test_explicit_env_wins(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _clear_repo_env(monkeypatch)
        monkeypatch.setenv("CONTRACT_REPO_DIR", "/explicit/occ")
        # Even with a contract under a different OCC tree, the explicit env wins.
        contract = _make_occ_tree(tmp_path, "OMN-1")
        assert (
            EvidenceCollector()._resolve_contract_repo_dir(contract) == "/explicit/occ"
        )

    def test_derived_from_occ_contract_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _clear_repo_env(monkeypatch)
        contract = _make_occ_tree(tmp_path, "OMN-2")
        expected = str(tmp_path / "onex_change_control")
        assert EvidenceCollector()._resolve_contract_repo_dir(contract) == expected

    def test_onex_cc_repo_path_fallback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _clear_repo_env(monkeypatch)
        monkeypatch.setenv("ONEX_CC_REPO_PATH", "/cc/repo")
        # Contract not under an OCC tree -> path derivation misses, env used.
        plain = tmp_path / "OMN-3.yaml"
        plain.write_text("ticket_id: OMN-3\n", encoding="utf-8")
        assert EvidenceCollector()._resolve_contract_repo_dir(plain) == "/cc/repo"

    def test_omni_home_fallback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _clear_repo_env(monkeypatch)
        (tmp_path / "onex_change_control").mkdir()
        monkeypatch.setenv("OMNI_HOME", str(tmp_path))
        plain = tmp_path / "OMN-4.yaml"
        plain.write_text("ticket_id: OMN-4\n", encoding="utf-8")
        assert EvidenceCollector()._resolve_contract_repo_dir(plain) == str(
            tmp_path / "onex_change_control"
        )

    def test_unresolvable_returns_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _clear_repo_env(monkeypatch)
        plain = tmp_path / "OMN-5.yaml"
        plain.write_text("ticket_id: OMN-5\n", encoding="utf-8")
        assert EvidenceCollector()._resolve_contract_repo_dir(plain) is None

    def test_omni_home_fallback_skipped_when_dir_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _clear_repo_env(monkeypatch)
        # OMNI_HOME set but no onex_change_control dir under it -> unresolvable.
        monkeypatch.setenv("OMNI_HOME", str(tmp_path))
        plain = tmp_path / "OMN-6.yaml"
        plain.write_text("ticket_id: OMN-6\n", encoding="utf-8")
        assert EvidenceCollector()._resolve_contract_repo_dir(plain) is None


# ---------------------------------------------------------------------------
# End-to-end: receipt check passes WITHOUT the caller exporting CONTRACT_REPO_DIR
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestContractRepoDirInjection:
    def test_receipt_check_verifies_without_exported_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The ticket's exact false-negative: a receipt-backed check verifies
        with CONTRACT_REPO_DIR *unset* because the node derives it from the OCC
        contract path and injects it into the subprocess env."""
        _clear_repo_env(monkeypatch)
        contract = _make_occ_tree(tmp_path, "OMN-13774x", status="PASS")

        results = EvidenceCollector().collect("OMN-13774x", str(contract))

        assert len(results) == 1
        assert results[0].status == EnumEvidenceCheckStatus.VERIFIED, results[0].message

    def test_non_pass_receipt_still_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Determinism must not paper over a real FAIL: a non-PASS receipt still
        fails the check even though the token now resolves."""
        _clear_repo_env(monkeypatch)
        contract = _make_occ_tree(tmp_path, "OMN-13774y", status="FAIL")

        results = EvidenceCollector().collect("OMN-13774y", str(contract))

        assert results[0].status == EnumEvidenceCheckStatus.FAILED

    def test_unresolvable_token_yields_fail_not_crash(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When the OCC root cannot be resolved (contract outside OCC, no env),
        the token stays unset and the check FAILS closed — the safe direction for
        a blocking gate, and never a crash."""
        _clear_repo_env(monkeypatch)
        # PASS receipt exists, but the contract is placed OUTSIDE any OCC tree
        # and no resolving env is set, so $CONTRACT_REPO_DIR cannot be derived.
        occ = tmp_path / "onex_change_control"
        receipt = occ / "drift" / "dod_receipts" / "OMN-7" / "item-a" / "command.yaml"
        receipt.parent.mkdir(parents=True)
        receipt.write_text("status: PASS\n", encoding="utf-8")
        contract = {
            "schema_version": "1.0.0",
            "ticket_id": "OMN-7",
            "dod_evidence": [
                {
                    "id": "dod-receipt",
                    "description": "receipt is PASS",
                    "checks": [
                        {
                            "check_type": "command",
                            "check_value": (
                                "grep -q '^status: PASS$' "
                                '"$CONTRACT_REPO_DIR/drift/dod_receipts/OMN-7/'
                                'item-a/command.yaml"'
                            ),
                        }
                    ],
                }
            ],
        }
        # Contract path is directly under tmp_path (no onex_change_control part).
        contract_path = tmp_path / "OMN-7.yaml"
        contract_path.write_text(yaml.dump(contract), encoding="utf-8")

        results = EvidenceCollector().collect("OMN-7", str(contract_path))

        assert results[0].status == EnumEvidenceCheckStatus.FAILED
