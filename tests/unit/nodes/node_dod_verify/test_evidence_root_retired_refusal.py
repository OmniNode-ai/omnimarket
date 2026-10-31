# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""node_dod_verify refuses a receipt root inside a retired repository (OMN-21776).

The command line wrote ``dod_report.json`` and ``dod_retry_state.json`` under
``ONEX_EVIDENCE_ROOT``, and a host env file that still named the retired
onex_change_control clone kept recreating 36 live evidence directories there, so
the workspace reconcile could never retire it. The writer now refuses such a root
or ``--output-path`` before it writes anything, and names the declared home. The
retired directory names and the home come from ``configs/runtime_paths.yaml``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from omnimarket.handlers import runtime_paths
from omnimarket.nodes.node_dod_verify.services.evidence_home import (
    EvidenceRootRetiredError,
    declared_evidence_home,
    refuse_retired_path,
)
from tests.unit.nodes.node_dod_verify.test_main_receipt_write import (
    _run_main,
    _write_contract,
)

_TICKET = "OMN-21776"


@pytest.mark.unit
class TestWriterRefusesRetiredRoot:
    def test_env_root_inside_retired_directory_writes_nothing(
        self, tmp_path: Path
    ) -> None:
        contract = _write_contract(tmp_path, ticket_id=_TICKET)
        retired = tmp_path / "registry" / "onex_change_control" / "evidence"

        result = _run_main(
            ticket_id=_TICKET, contract_path=contract, evidence_root=retired
        )

        assert result.returncode == 2, result.stderr
        assert "onex_change_control" in result.stderr
        assert "ONEX_EVIDENCE_ROOT" in result.stderr
        assert not (tmp_path / "registry").exists(), (
            "the retired directory was recreated"
        )

    def test_output_path_inside_retired_directory_writes_nothing(
        self, tmp_path: Path
    ) -> None:
        contract = _write_contract(tmp_path, ticket_id=_TICKET)
        target = (
            tmp_path / "onex_change_control" / "evidence" / _TICKET / "dod_report.json"
        )

        result = _run_main(
            ticket_id=_TICKET,
            contract_path=contract,
            evidence_root=None,
            output_path=target,
        )

        assert result.returncode == 2, result.stderr
        assert "--output-path" in result.stderr
        assert not (tmp_path / "onex_change_control").exists()

    def test_message_names_the_declared_home(self, tmp_path: Path) -> None:
        contract = _write_contract(tmp_path, ticket_id=_TICKET)
        retired = tmp_path / "onex_change_control" / "evidence"

        result = _run_main(
            ticket_id=_TICKET, contract_path=contract, evidence_root=retired
        )

        home_subdir = runtime_paths.runtime_path("dod_evidence.home_subdir")
        assert f"/{home_subdir}" in result.stderr

    def test_control_a_root_under_the_declared_home_still_writes(
        self, tmp_path: Path
    ) -> None:
        contract = _write_contract(tmp_path, ticket_id=_TICKET)
        home = (
            tmp_path / "state" / runtime_paths.runtime_path("dod_evidence.home_subdir")
        )

        result = _run_main(
            ticket_id=_TICKET, contract_path=contract, evidence_root=home
        )

        assert result.returncode == 0, result.stderr
        assert (home / _TICKET / "dod_report.json").is_file()
        assert (home / _TICKET / "dod_retry_state.json").is_file()


@pytest.mark.unit
class TestRefusalIsDeclaredNotHardCoded:
    def test_declared_retired_names_drive_the_refusal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        authority: dict[str, Any] = {
            "dod_evidence": {"home_subdir": "evidence", "retired_roots": ["gone_repo"]}
        }
        monkeypatch.setattr(runtime_paths, "_load_authority", lambda: authority)

        with pytest.raises(EvidenceRootRetiredError, match="gone_repo"):
            refuse_retired_path(tmp_path / "gone_repo" / "evidence", source="X")
        # A name the authority no longer lists is admitted: nothing is hard-coded.
        refuse_retired_path(tmp_path / "onex_change_control" / "evidence", source="X")

    def test_symlink_into_a_retired_directory_is_refused(self, tmp_path: Path) -> None:
        retired = tmp_path / "onex_change_control"
        retired.mkdir()
        link = tmp_path / "evidence_link"
        link.symlink_to(retired)

        with pytest.raises(EvidenceRootRetiredError):
            refuse_retired_path(link / "evidence", source="ONEX_EVIDENCE_ROOT")

    def test_home_sits_under_the_state_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ONEX_STATE_DIR", str(tmp_path))
        assert declared_evidence_home() == str(
            tmp_path / runtime_paths.runtime_path("dod_evidence.home_subdir")
        )

    def test_home_names_the_state_keys_when_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ONEX_STATE_DIR", raising=False)
        monkeypatch.delenv("ONEX_STATE_ROOT", raising=False)
        assert "ONEX_STATE_DIR" in declared_evidence_home()
