# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``EvidenceCollector._find_contract`` searches only its declared roots.

The contract search roots are ``_DEFAULT_CONTRACT_ROOTS`` (after a materialised
dev worktree). ``_find_contract`` used to repeat the ``$OMNI_HOME`` root as a
hand-built fallback; with ``OMNI_HOME`` unset that fallback became a path
relative to the working directory, so a change-control checkout under the
caller's cwd was read as the contract source. The duplicate is gone
(OMN-17427): the declared roots decide, and nothing else.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_dod_verify.services.evidence_collector import (
    EvidenceCollector,
)

_TICKET = "OMN-1"


def _write_contract(root: Path) -> Path:
    contracts = root / "onex_change_control" / "contracts"
    contracts.mkdir(parents=True)
    path = contracts / f"{_TICKET}.yaml"
    path.write_text("ticket_id: OMN-1\n", encoding="utf-8")
    return path


@pytest.mark.unit
def test_omni_home_unset_does_not_search_working_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _write_contract(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OMNI_HOME", raising=False)
    monkeypatch.delenv("ONEX_CC_REPO_PATH", raising=False)

    assert EvidenceCollector()._find_contract(_TICKET) is None


@pytest.mark.unit
def test_omni_home_root_still_resolves(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Positive control: the declared $OMNI_HOME root still finds the contract.
    expected = _write_contract(tmp_path)
    monkeypatch.chdir(tmp_path.parent)
    monkeypatch.setenv("OMNI_HOME", str(tmp_path))
    monkeypatch.delenv("ONEX_CC_REPO_PATH", raising=False)

    assert EvidenceCollector()._find_contract(_TICKET) == expected


@pytest.mark.unit
def test_onex_cc_repo_path_root_still_resolves(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Positive control: the declared ONEX_CC_REPO_PATH root still wins.
    expected = _write_contract(tmp_path)
    monkeypatch.chdir(tmp_path.parent)
    monkeypatch.delenv("OMNI_HOME", raising=False)
    monkeypatch.setenv("ONEX_CC_REPO_PATH", str(tmp_path / "onex_change_control"))

    assert EvidenceCollector()._find_contract(_TICKET) == expected
