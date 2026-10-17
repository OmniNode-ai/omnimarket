# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``EvidenceCollector._find_contract`` searches only its declared roots.

The contract search roots are ``_DEFAULT_CONTRACT_ROOTS`` (after a materialised
dev worktree). ``_find_contract`` used to repeat the ``$OMNI_HOME`` root as a
hand-built fallback; with ``OMNI_HOME`` unset that fallback became a path
relative to the working directory, so a change-control checkout under the
caller's cwd was read as the contract source. The duplicate is gone
(OMN-17427): the declared roots decide, and nothing else.

``$OMNI_HOME`` is no longer one of those roots. The change-control clone is
located only through ``ONEX_CC_REPO_PATH``, or derived from a contract path
that already lies inside it, the rule the dod and integration sweeps follow,
so no resolver in the collector spells where that clone lives.
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
def test_omni_home_alone_does_not_locate_contract(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _write_contract(tmp_path)
    monkeypatch.chdir(tmp_path.parent)
    monkeypatch.setenv("OMNI_HOME", str(tmp_path))
    monkeypatch.delenv("ONEX_CC_REPO_PATH", raising=False)

    assert EvidenceCollector()._find_contract(_TICKET) is None


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


def _only_registry_root(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    monkeypatch.setenv("OMNI_HOME", str(root))
    monkeypatch.delenv("ONEX_CC_REPO_PATH", raising=False)
    monkeypatch.delenv("CONTRACT_REPO_DIR", raising=False)


@pytest.mark.unit
def test_omni_home_alone_does_not_locate_occ_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _write_contract(tmp_path)
    _only_registry_root(monkeypatch, tmp_path)

    assert EvidenceCollector()._resolve_occ_root() is None


@pytest.mark.unit
def test_onex_cc_repo_path_locates_occ_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Positive control: the explicit locator still resolves.
    _write_contract(tmp_path)
    _only_registry_root(monkeypatch, tmp_path)
    monkeypatch.setenv("ONEX_CC_REPO_PATH", str(tmp_path / "onex_change_control"))

    assert EvidenceCollector()._resolve_occ_root() == tmp_path / "onex_change_control"


@pytest.mark.unit
def test_omni_home_alone_does_not_locate_contract_repo_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _write_contract(tmp_path)
    _only_registry_root(monkeypatch, tmp_path)
    plain = tmp_path / f"{_TICKET}.yaml"
    plain.write_text("ticket_id: OMN-1\n", encoding="utf-8")

    assert EvidenceCollector()._resolve_contract_repo_dir(plain) is None


@pytest.mark.unit
def test_occ_cwd_is_the_tree_the_contract_was_read_from(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The grep cwd comes from the contract's own tree, never from $OMNI_HOME.
    elsewhere = tmp_path / "elsewhere"
    contract = _write_contract(elsewhere)
    home = tmp_path / "home"
    _write_contract(home)
    _only_registry_root(monkeypatch, home)

    assert EvidenceCollector()._infer_occ_cwd(contract) == str(
        elsewhere / "onex_change_control"
    )


@pytest.mark.unit
def test_occ_cwd_none_for_contract_outside_occ_tree(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _write_contract(tmp_path)
    _only_registry_root(monkeypatch, tmp_path)
    plain = tmp_path / f"{_TICKET}.yaml"
    plain.write_text("ticket_id: OMN-1\n", encoding="utf-8")

    assert EvidenceCollector()._infer_occ_cwd(plain) is None
