# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for NodeContractSweep handler.

Covers:
- .venv / site-packages paths are excluded from scanning (OMN-9445)
- Valid topic names pass without violation
- Invalid topic names (wrong kind segment, or malformed segment count) produce
  INVALID_TOPIC_NAME violations; the `snapshot` kind is a legitimate,
  variable-depth projection-broadcast topic convention (OMN-14544)
- Missing required fields produce MISSING_REQUIRED_FIELD violations
- OMN-14542 (class fix, parent OMN-14531): `repos` is required (no default
  empty-scan-everything), `OMNI_HOME` is required (no `__file__`-relative
  guess), and a scope that resolves to zero contracts is a hard ERROR verdict
  — never a silent, empty-but-successful PASS. The former
  ``test_venv_path_skipped`` / ``test_site_packages_path_skipped`` scenarios
  are exactly the "EXISTS-but-WRONG scope" case (a real repo dir exists but
  every contract under it is excluded) and are repurposed below as the
  MANDATORY RED proof.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_contract_sweep.handlers.handler_contract_sweep import (
    ContractSweepRequest,
    EnumSweepStatus,
    EnumViolationType,
    NodeContractSweep,
)


def _write_contract(base: Path, node_name: str, content: str) -> Path:
    """Write a contract.yaml under base/nodes/<node_name>/contract.yaml."""
    node_dir = base / "nodes" / node_name
    node_dir.mkdir(parents=True, exist_ok=True)
    contract = node_dir / "contract.yaml"
    contract.write_text(content)
    return contract


_VALID_CONTRACT = textwrap.dedent("""\
    name: node_test_valid
    node_type: COMPUTE_GENERIC
    contract_version:
      major: 1
      minor: 0
      patch: 0
    node_version: "1.0.0"
    description: "A valid test node"
    event_bus:
      publish_topics:
        - "onex.evt.platform.test-event.v1"
      subscribe_topics:
        - "onex.cmd.platform.test-cmd.v1"
""")

_SNAPSHOT_KIND_CONTRACT = textwrap.dedent("""\
    name: node_test_snapshot
    node_type: ORCHESTRATOR_GENERIC
    contract_version:
      major: 1
      minor: 0
      patch: 0
    node_version: "1.0.0"
    description: "Node with snapshot-kind topic (legitimate projection-broadcast convention, OMN-14544)"
    event_bus:
      publish_topics:
        - "onex.snapshot.platform.registration-snapshots.v1"
""")

_CONTROL_PLANE_SUBSCRIBE_CONTRACT = textwrap.dedent("""\
    name: node_test_control_plane_consumer
    node_type: REDUCER_GENERIC
    contract_version:
      major: 1
      minor: 0
      patch: 0
    node_version: "1.0.0"
    description: "Consumes the onex-api-owned tenant lifecycle topic (OMN-16930)"
    event_bus:
      subscribe_topics:
        - "onex.tenant.events"
""")

_CONTROL_PLANE_PUBLISH_CONTRACT = textwrap.dedent("""\
    name: node_test_control_plane_producer
    node_type: EFFECT_GENERIC
    contract_version:
      major: 1
      minor: 0
      patch: 0
    node_version: "1.0.0"
    description: "Illegitimately declares the onex-api-owned topic as an OUTPUT"
    event_bus:
      publish_topics:
        - "onex.tenant.events"
""")

_MALFORMED_SNAPSHOT_CONTRACT = textwrap.dedent("""\
    name: node_test_malformed_snapshot
    node_type: ORCHESTRATOR_GENERIC
    contract_version:
      major: 1
      minor: 0
      patch: 0
    node_version: "1.0.0"
    description: "Node with a snapshot topic missing the producer segment"
    event_bus:
      publish_topics:
        - "onex.snapshot.v1"
""")


@pytest.mark.unit
class TestHandlerContractSweepVenvExclusion:
    """OMN-9445: .venv and site-packages paths must be excluded from scanning."""

    def test_venv_only_scope_is_red_not_silent_pass(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """MANDATORY RED PROOF (OMN-14542): a requested repo that exists on
        disk but whose only contract lives under an excluded .venv path is
        the "EXISTS-but-WRONG scope" case — scanned_count == 0 after
        exclusion. This must be a hard ERROR verdict, never a clean,
        empty-but-successful PASS (the exact failure mode behind the "9
        contracts clean while 941 exist" false-clean receipt)."""
        monkeypatch.setenv("OMNI_HOME", str(tmp_path))

        # Repo with a valid src structure so it's picked up by repo discovery
        repo = tmp_path / "some_repo"
        (repo / "src").mkdir(parents=True)

        # Plant a contract inside .venv — well-formed but should be excluded from the scan by path alone
        venv_node = repo / ".venv" / "lib" / "python3.12" / "site-packages"
        _write_contract(venv_node, "node_bad_venv", _SNAPSHOT_KIND_CONTRACT)

        result = NodeContractSweep().handle(ContractSweepRequest(repos=["some_repo"]))

        assert result.contracts_checked == 0
        assert result.scanned_count == 0
        assert result.status == EnumSweepStatus.ERROR
        assert result.scope_error != ""
        assert result.violations == []

    def test_site_packages_only_scope_is_red(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Same RED case, site-packages-without-.venv-prefix variant."""
        monkeypatch.setenv("OMNI_HOME", str(tmp_path))

        repo = tmp_path / "some_repo"
        (repo / "src").mkdir(parents=True)

        # site-packages without .venv prefix (edge case)
        pkg_dir = repo / "lib" / "site-packages"
        _write_contract(pkg_dir, "node_bad_pkg", _SNAPSHOT_KIND_CONTRACT)

        result = NodeContractSweep().handle(ContractSweepRequest(repos=["some_repo"]))

        assert result.contracts_checked == 0
        assert result.scanned_count == 0
        assert result.status == EnumSweepStatus.ERROR
        assert result.violations == []

    def test_source_contract_is_scanned(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GREEN PROOF: a genuinely-healthy populated scope (contract under
        src/, not .venv) reports PASS with scanned_count == 1."""
        monkeypatch.setenv("OMNI_HOME", str(tmp_path))

        repo = tmp_path / "some_repo"
        src = repo / "src"
        src.mkdir(parents=True)
        _write_contract(src, "node_valid_src", _VALID_CONTRACT)

        result = NodeContractSweep().handle(ContractSweepRequest(repos=["some_repo"]))

        assert result.contracts_checked == 1
        assert result.scanned_count == 1
        assert result.status == EnumSweepStatus.PASS
        assert result.violations == []

    def test_venv_skipped_while_src_scanned(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When both .venv and src/ contracts exist, only src/ is counted,
        and the non-zero scanned_count still reports a clean PASS."""
        monkeypatch.setenv("OMNI_HOME", str(tmp_path))

        repo = tmp_path / "some_repo"
        src = repo / "src"
        src.mkdir(parents=True)

        # Good contract in src/
        _write_contract(src, "node_valid_src", _VALID_CONTRACT)

        # Contract in .venv — well-formed but should be ignored by path exclusion alone
        venv_node = repo / ".venv" / "lib" / "python3.12" / "site-packages"
        _write_contract(venv_node, "node_bad_venv", _SNAPSHOT_KIND_CONTRACT)

        result = NodeContractSweep().handle(ContractSweepRequest(repos=["some_repo"]))

        assert result.contracts_checked == 1
        assert result.scanned_count == 1
        assert result.status == EnumSweepStatus.PASS
        assert result.violations == []


@pytest.mark.unit
class TestHandlerContractSweepRequiredCensus:
    """OMN-14542: repos is required; OMNI_HOME is required; a requested repo
    absent on disk is a hard ERROR (silent narrowing is the class defect)."""

    def test_repos_field_is_required(self) -> None:
        with pytest.raises(ValidationError):
            ContractSweepRequest()  # type: ignore[call-arg]

    def test_repos_field_rejects_empty_list(self) -> None:
        with pytest.raises(ValidationError):
            ContractSweepRequest(repos=[])

    def test_missing_omni_home_env_is_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("OMNI_HOME", raising=False)

        result = NodeContractSweep().handle(ContractSweepRequest(repos=["some_repo"]))

        assert result.status == EnumSweepStatus.ERROR
        assert result.scanned_count == 0
        assert "OMNI_HOME" in result.scope_error

    def test_typo_repo_name_resolving_to_zero_repos_is_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """MANDATORY RED PROOF: a syntactically valid --repos value pointing
        at a repo name that doesn't exist under OMNI_HOME must be an ERROR,
        not a silently-narrowed empty PASS."""
        monkeypatch.setenv("OMNI_HOME", str(tmp_path))

        result = NodeContractSweep().handle(
            ContractSweepRequest(repos=["does_not_exist_repo"])
        )

        assert result.status == EnumSweepStatus.ERROR
        assert result.scanned_count == 0
        assert result.missing_repos == ["does_not_exist_repo"]

    def test_one_bad_repo_among_good_repos_is_still_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A partially-narrowed multi-repo request (one typo'd entry mixed
        with valid entries) must ALSO refuse PASS — this is the literal "9
        vs 941" shape: most of the requested scope silently drops out."""
        monkeypatch.setenv("OMNI_HOME", str(tmp_path))
        _write_contract(tmp_path / "good_repo" / "src", "node_x", _VALID_CONTRACT)

        result = NodeContractSweep().handle(
            ContractSweepRequest(repos=["good_repo", "typo_repo"])
        )

        assert result.status == EnumSweepStatus.ERROR
        assert result.missing_repos == ["typo_repo"]
        # The scan still ran over the resolvable repo, but the verdict must
        # not be reported as a trustworthy PASS/FAIL.
        assert result.contracts_checked == 1


@pytest.mark.unit
class TestHandlerContractSweepTopicValidation:
    """Topic naming validation covers cmd|evt|intent (fixed producer.event
    shape) and snapshot (variable-depth path, OMN-14544 — verified in live
    use across omnimarket, omnidash, onex_change_control, omnibase_infra,
    omnibase_core, and omniintelligence as a real projection-broadcast
    convention, not a naming defect)."""

    def test_valid_evt_topic_passes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OMNI_HOME", str(tmp_path))
        repo = tmp_path / "repo"
        (repo / "src").mkdir(parents=True)
        _write_contract(repo / "src", "node_valid", _VALID_CONTRACT)

        result = NodeContractSweep().handle(ContractSweepRequest(repos=["repo"]))
        assert result.violations == []

    def test_snapshot_kind_topic_passes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GREEN PROOF (OMN-14544): a well-formed snapshot topic
        (onex.snapshot.<producer>.<path...>.vN) is a legitimate
        projection-broadcast topic, not a violation."""
        monkeypatch.setenv("OMNI_HOME", str(tmp_path))
        repo = tmp_path / "repo"
        (repo / "src").mkdir(parents=True)
        _write_contract(repo / "src", "node_snapshot", _SNAPSHOT_KIND_CONTRACT)

        result = NodeContractSweep().handle(ContractSweepRequest(repos=["repo"]))
        assert result.contracts_checked == 1
        assert result.violations == []
        assert result.status == EnumSweepStatus.PASS

    def test_malformed_snapshot_topic_produces_violation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """MANDATORY RED PROOF (OMN-14544): accepting the snapshot kind must
        not degrade into accept-anything — a snapshot topic missing the
        producer segment still fails."""
        monkeypatch.setenv("OMNI_HOME", str(tmp_path))
        repo = tmp_path / "repo"
        (repo / "src").mkdir(parents=True)
        _write_contract(
            repo / "src", "node_malformed_snapshot", _MALFORMED_SNAPSHOT_CONTRACT
        )

        result = NodeContractSweep().handle(ContractSweepRequest(repos=["repo"]))
        assert result.contracts_checked == 1
        topic_violations = [
            v
            for v in result.violations
            if v.violation_type == EnumViolationType.INVALID_TOPIC_NAME
        ]
        assert len(topic_violations) == 1
        assert "onex.snapshot.v1" in topic_violations[0].message

    def test_control_plane_topic_passes_as_a_subscription(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GREEN PROOF (OMN-16930): onex.tenant.events is onex-api's
        control-plane tenant-lifecycle topic. It predates the
        onex.{cmd|evt|intent}.<producer>.<event>.vN convention and this repo
        only CONSUMES it, so a subscription to it is exempt."""
        monkeypatch.setenv("OMNI_HOME", str(tmp_path))
        repo = tmp_path / "repo"
        (repo / "src").mkdir(parents=True)
        _write_contract(
            repo / "src", "node_cp_consumer", _CONTROL_PLANE_SUBSCRIBE_CONTRACT
        )

        result = NodeContractSweep().handle(ContractSweepRequest(repos=["repo"]))
        assert result.contracts_checked == 1
        assert result.violations == []
        assert result.status == EnumSweepStatus.PASS

    def test_control_plane_topic_is_rejected_as_a_publish_target(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """MANDATORY RED PROOF (OMN-17288): the exemption is DIRECTIONAL.

        The exception exists because onex-api owns onex.tenant.events and this
        repo consumes it. Applied to publish_topics it excuses the opposite
        claim -- an omnimarket node asserting ownership of another repo's
        control-plane topic -- and that contract would pass the naming lint
        unnoticed. Ownership is the whole reason the name is exempt, so the
        exemption cannot survive a direction flip.
        """
        monkeypatch.setenv("OMNI_HOME", str(tmp_path))
        repo = tmp_path / "repo"
        (repo / "src").mkdir(parents=True)
        _write_contract(
            repo / "src", "node_cp_producer", _CONTROL_PLANE_PUBLISH_CONTRACT
        )

        result = NodeContractSweep().handle(ContractSweepRequest(repos=["repo"]))
        assert result.contracts_checked == 1
        topic_violations = [
            v
            for v in result.violations
            if v.violation_type == EnumViolationType.INVALID_TOPIC_NAME
        ]
        assert len(topic_violations) == 1, (
            "a node declaring onex.tenant.events as a PUBLISH target must be "
            "flagged: the control-plane exemption is a consumer carve-out, "
            "not a licence to produce onto another repo's topic"
        )
        assert topic_violations[0].field == "event_bus.publish_topics"
        assert "onex.tenant.events" in topic_violations[0].message

    def test_missing_required_fields_produces_violations(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OMNI_HOME", str(tmp_path))
        repo = tmp_path / "repo"
        (repo / "src").mkdir(parents=True)
        _write_contract(
            repo / "src",
            "node_incomplete",
            "name: node_incomplete\n",  # Missing all other required fields
        )

        result = NodeContractSweep().handle(ContractSweepRequest(repos=["repo"]))
        assert result.contracts_checked == 1
        missing = [
            v
            for v in result.violations
            if v.violation_type == EnumViolationType.MISSING_REQUIRED_FIELD
        ]
        missing_names = {v.field for v in missing}
        assert "contract_version" in missing_names
        assert "node_type" in missing_names
        assert "node_version" in missing_names
        assert "description" in missing_names
