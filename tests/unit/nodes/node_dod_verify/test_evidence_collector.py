# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Tests for EvidenceCollector — contract loading and check execution."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

from omnimarket.nodes.node_dod_verify.handlers.handler_dod_verify import (
    HandlerDodVerify,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_state import (
    EnumDodVerifyStatus,
    EnumEvidenceCheckStatus,
)
from omnimarket.nodes.node_dod_verify.services.evidence_collector import (
    EvidenceCollector,
)


def _write_contract(
    tmp_path: Path,
    ticket_id: str = "OMN-TEST",
    dod_evidence: list[dict] | None = None,
) -> Path:
    """Write a minimal contract YAML and return its path."""
    contract = {
        "schema_version": "1.0.0",
        "ticket_id": ticket_id,
        "dod_evidence": dod_evidence or [],
    }
    p = tmp_path / f"{ticket_id}.yaml"
    p.write_text(yaml.dump(contract), encoding="utf-8")
    return p


@pytest.mark.unit
class TestEvidenceCollector:
    """Unit tests for EvidenceCollector."""

    @pytest.mark.parametrize("no_checkout", [False, True])
    @pytest.mark.parametrize("check_type", ["command", "test_passes"])
    def test_sibling_cwd_missing_checkout_refused(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        no_checkout: bool,
        check_type: str,
    ) -> None:
        """Absent and no-checkout siblings are setup refusals, never failures."""
        from omnimarket.nodes.node_dod_verify.models.model_dod_verify_state import (
            EnumEvidenceUnverifiableCause,
        )

        home = tmp_path / "registry"
        contracts = home / "omniintelligence" / "contracts"
        contracts.mkdir(parents=True)
        if no_checkout:
            source = tmp_path / "source"
            source.mkdir()
            subprocess.run(
                ["git", "init", str(source)],
                check=True,
                capture_output=True,
                env=scrub_git_location_env(os.environ),
            )
            (source / "tracked.txt").write_text("checkout required\n")
            subprocess.run(
                ["git", "-C", str(source), "add", "."],
                check=True,
                env=scrub_git_location_env(os.environ),
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(source),
                    "-c",
                    "user.name=Fixture",
                    "-c",
                    "user.email=fixture@example.test",
                    "commit",
                    "-m",
                    "fixture",
                ],
                check=True,
                capture_output=True,
                env=scrub_git_location_env(os.environ),
            )
            subprocess.run(
                [
                    "git",
                    "clone",
                    "--no-checkout",
                    str(source),
                    str(home / "omnibase_core"),
                ],
                check=True,
                capture_output=True,
                env=scrub_git_location_env(os.environ),
            )
        monkeypatch.setenv("OMNI_HOME", str(home))
        contract = _write_contract(
            contracts,
            dod_evidence=[
                {
                    "id": "sibling",
                    "description": "Sibling check",
                    "checks": [
                        {
                            "check_type": check_type,
                            "check_value": "true",
                            "cwd": "${OMNI_HOME}/omnibase_core",
                        }
                    ],
                }
            ],
        )
        collector = EvidenceCollector()
        results = collector.collect("OMN-TEST", contract_path=str(contract))
        assert results[0].status == EnumEvidenceCheckStatus.SKIPPED
        assert (
            results[0].unverifiable_cause
            == EnumEvidenceUnverifiableCause.SIBLING_CHECKOUT_MISSING
        )
        assert "sibling checkout is missing" in (results[0].message or "")
        assert "omnibase_core" in (results[0].message or "")

    @pytest.mark.parametrize(
        "pin_kind", ["source", "action", "checkout", "conflicting", "default"]
    )
    def test_sibling_cwd_replay_checkout_revision(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        pin_kind: str,
    ) -> None:
        """Replay uses the head's code pin, otherwise history at merge time."""
        from omnimarket.nodes.node_dod_verify.handlers import (
            handler_occ_replay as replay,
        )

        source = tmp_path / "canonical" / "omnibase_core"
        source.mkdir(parents=True)

        def git(*args: str) -> str:
            return subprocess.run(
                ["git", "-C", str(source), *args],
                check=True,
                capture_output=True,
                text=True,
                env=scrub_git_location_env(os.environ),
            ).stdout.strip()

        git("init")
        (source / "tracked.txt").write_text("at merge\n")
        git("add", ".")
        for stamp, content in [
            ("2020-01-01T00:00:00Z", "at merge\n"),
            ("2020-01-03T00:00:00Z", "later\n"),
        ]:
            (source / "tracked.txt").write_text(content)
            git("add", ".")
            monkeypatch.setenv("GIT_AUTHOR_DATE", stamp)
            monkeypatch.setenv("GIT_COMMITTER_DATE", stamp)
            git(
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.test",
                "commit",
                "-m",
                content.strip(),
            )
        earlier, later = git("rev-parse", "HEAD~1"), git("rev-parse", "HEAD")
        branch = git("branch", "--show-current")
        git("update-ref", f"refs/remotes/origin/{branch}", later)
        git("symbolic-ref", "refs/remotes/origin/HEAD", f"refs/remotes/origin/{branch}")
        monkeypatch.setattr(replay, "canonical_clone", lambda _repo: source)
        home = tmp_path / "trees" / "head"
        tree = home / "omniintelligence"
        contracts = tree / "contracts"
        contracts.mkdir(parents=True)
        contract = _write_contract(
            contracts,
            dod_evidence=[
                {
                    "id": "sibling",
                    "description": "Sibling check",
                    "checks": [
                        {
                            "check_type": "command",
                            "check_value": "true",
                            "cwd": "${OMNI_HOME}/omnibase_core",
                        }
                    ],
                }
            ],
        )
        if pin_kind == "source":
            (tree / "pyproject.toml").write_text(
                '[tool.uv.sources]\nomnibase-core = { git = "https://github.com/OmniNode-ai/omnibase_core.git", '
                f'rev = "{later}" }}\n'
            )
        elif pin_kind in {"action", "checkout", "conflicting"}:
            workflows = tree / ".github" / "workflows"
            workflows.mkdir(parents=True)
            if pin_kind == "checkout":
                (workflows / "ci.yml").write_text(
                    "jobs:\n  check:\n    steps:\n      - uses: actions/checkout@v4\n"
                    f"        with:\n          repository: OmniNode-ai/omnibase_core\n          ref: {later}\n"
                )
            else:
                (workflows / "ci.yml").write_text(
                    f"jobs:\n  check:\n    steps:\n      - uses: OmniNode-ai/omnibase_core/.github/actions/check@{later}\n"
                )
            if pin_kind == "conflicting":
                (workflows / "other.yml").write_text(
                    f"jobs:\n  check:\n    steps:\n      - uses: OmniNode-ai/omnibase_core/.github/actions/check@{earlier}\n"
                )
                with pytest.raises(RuntimeError, match="conflicting code pins"):
                    replay.HandlerOccReplay._prepare_sibling_checkouts(
                        "OmniNode-ai", tree, home, [contract], "2020-01-02T00:00:00Z"
                    )
                assert not (home / "omnibase_core").exists()
                return
        replay.HandlerOccReplay._prepare_sibling_checkouts(
            "OmniNode-ai", tree, home, [contract], "2020-01-02T00:00:00Z"
        )
        sibling = home / "omnibase_core"
        rc, actual = replay._git(sibling, "rev-parse", "HEAD")
        assert rc == 0
        assert actual.strip() == (earlier if pin_kind == "default" else later)
        assert (sibling / "tracked.txt").is_file()
        assert (sibling / "tracked.txt").stat().st_mode & 0o222 == 0
        assert sibling.stat().st_mode & 0o222 == 0
        replay.HandlerOccReplay._restore_sibling_permissions(home, tree.name)

    def test_sibling_cwd_replay_hosted_verifier(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Replay provisions a sibling; disabling that input refuses the head."""
        from omnimarket.nodes.node_dod_verify.handlers import (
            handler_occ_replay as replay,
        )
        from omnimarket.nodes.node_dod_verify.models.model_occ_replay import (
            ModelOccReplayRequest,
        )

        def git(repo: Path, *args: str) -> str:
            return subprocess.run(
                ["git", "-C", str(repo), *args],
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
                env=scrub_git_location_env(os.environ),
            ).stdout.strip()

        def commit(repo: Path) -> str:
            git(repo, "add", ".")
            git(
                repo,
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.test",
                "commit",
                "-m",
                "fixture",
            )
            return git(repo, "rev-parse", "HEAD")

        # Local object sources are outside the replay environment. Its head
        # and control homes start with only the PR's own repository present.
        sibling_source = tmp_path / "objects" / "omnibase_core"
        sibling_source.mkdir(parents=True)
        git(sibling_source, "init")
        (sibling_source / "tracked.txt").write_text("at merge\n")
        sibling_sha = commit(sibling_source)
        own = tmp_path / "objects" / "omnimarket"
        own.mkdir()
        git(own, "init")
        (own / "README.md").write_text("fixture\n")
        base = commit(own)
        contracts = own / "contracts"
        contracts.mkdir()
        contract = _write_contract(
            contracts,
            dod_evidence=[
                {
                    "id": "sibling",
                    "description": "Pinned sibling and changed product file",
                    "binds_ac": ["AC1"],
                    "checks": [
                        {
                            "check_type": "command",
                            "check_value": "grep -qx 'at merge' tracked.txt && test -f ../omnimarket/product.txt",
                            "cwd": "${OMNI_HOME}/omnibase_core",
                        }
                    ],
                }
            ],
        )
        data = yaml.safe_load(contract.read_text())
        data["requirements"] = [
            {
                "id": "req-sibling",
                "statement": "Replay provisions the pinned sibling.",
                "acceptance": [
                    {
                        "id": "AC1",
                        "statement": "AC1: The pinned sibling and product file are present -- falsifier: the sibling check fails without either file",
                    }
                ],
            }
        ]
        contract.write_text(yaml.safe_dump(data))
        (own / "pyproject.toml").write_text(
            '[tool.uv.sources]\nomnibase-core = { git = "https://github.com/OmniNode-ai/omnibase_core.git", '
            f'rev = "{sibling_sha}" }}\n'
        )
        (own / "product.txt").write_text("changed product\n")
        tests = own / "tests"
        tests.mkdir()
        (tests / "test_product.py").write_text("# test-side overlay fixture\n")
        head = commit(own)
        monkeypatch.setattr(
            replay, "canonical_clone", lambda _repository: sibling_source
        )
        monkeypatch.setenv("PYTHONPATH", str(Path(replay.__file__).parents[4]))
        pr = replay._MergedPr(
            number=1,
            merge_sha=head,
            parent=base,
            merged_at="2026-10-12T00:00:00Z",
            title="fixture",
        )
        prepared: list[str] = []
        prepare = replay.HandlerOccReplay._prepare_sibling_checkouts

        def provision(
            owner: str, tree: Path, home: Path, paths: list[Path], merged_at: str
        ) -> None:
            assert sorted(path.name for path in home.iterdir()) == ["omnimarket"]
            prepare(owner, tree, home, paths, merged_at)
            sibling = home / "omnibase_core"
            assert git(sibling, "rev-parse", "HEAD") == sibling_sha
            prepared.append(home.name)

        monkeypatch.setattr(
            replay.HandlerOccReplay,
            "_prepare_sibling_checkouts",
            staticmethod(provision),
        )
        request = ModelOccReplayRequest(
            repository="OmniNode-ai/omnimarket",
            work_dir=tmp_path / "replay",
            verifier_python=Path(sys.executable),
            verifier_timeout_s=20,
        )
        handler = replay.HandlerOccReplay()
        verdict, note = handler._new_path(
            request, own, own, request.work_dir, pr, head, "fixture", ("OMN-TEST",)
        )
        assert verdict is not None, note
        assert verdict.admitted, note
        assert prepared == ["head", "base"]
        dod = request.work_dir / "dod" / "1"
        head_result = json.loads((dod / "head-OMN-TEST.json").read_text())
        base_result = json.loads((dod / "base-OMN-TEST.json").read_text())
        assert head_result["status"] == "verified"
        assert base_result["status"] == "failed"
        assert base_result["checks"][0]["status"] == "failed"

        monkeypatch.setattr(
            replay.HandlerOccReplay,
            "_prepare_sibling_checkouts",
            staticmethod(lambda *_args: None),
        )
        verdict, note = handler._new_path(
            request, own, own, request.work_dir, pr, head, "fixture", ("OMN-TEST",)
        )
        assert verdict is not None, note
        assert not verdict.admitted, note
        refused = json.loads((dod / "head-OMN-TEST.json").read_text())
        assert refused["status"] != "verified"
        assert refused["checks"][0]["status"] == "skipped"
        assert refused["checks"][0]["unverifiable_cause"] == "sibling_checkout_missing"

    def test_explicit_contract_path(self, tmp_path: Path) -> None:
        """Collector loads contract from explicit path."""
        _write_contract(
            tmp_path,
            dod_evidence=[
                {
                    "id": "dod-001",
                    "description": "File exists",
                    "checks": [
                        {
                            "check_type": "command",
                            "command": "true",
                        }
                    ],
                }
            ],
        )
        collector = EvidenceCollector()
        results = collector.collect(
            "OMN-TEST",
            contract_path=str(tmp_path / "OMN-TEST.yaml"),
        )
        assert len(results) == 1
        assert results[0].evidence_id == "dod-001"
        assert results[0].status == EnumEvidenceCheckStatus.VERIFIED

    def test_missing_contract_file(self, tmp_path: Path) -> None:
        """Collector returns FAILED when contract file doesn't exist."""
        collector = EvidenceCollector()
        results = collector.collect(
            "OMN-NOPE",
            contract_path=str(tmp_path / "nonexistent.yaml"),
        )
        assert len(results) == 1
        assert results[0].status == EnumEvidenceCheckStatus.FAILED
        assert results[0].message == (
            "pull request cites OMN-NOPE but carries no contracts/OMN-NOPE.yaml"
        )

    def test_empty_dod_evidence(self, tmp_path: Path) -> None:
        """Contract with no dod_evidence -> single SKIPPED result."""
        _write_contract(tmp_path, dod_evidence=[])
        collector = EvidenceCollector()
        results = collector.collect(
            "OMN-TEST",
            contract_path=str(tmp_path / "OMN-TEST.yaml"),
        )
        assert len(results) == 1
        assert results[0].status == EnumEvidenceCheckStatus.SKIPPED

    def test_command_check_passes(self, tmp_path: Path) -> None:
        """Command check with exit 0 -> VERIFIED."""
        _write_contract(
            tmp_path,
            dod_evidence=[
                {
                    "id": "dod-001",
                    "description": "True check",
                    "checks": [{"check_type": "command", "command": "true"}],
                }
            ],
        )
        collector = EvidenceCollector()
        results = collector.collect(
            "OMN-TEST",
            contract_path=str(tmp_path / "OMN-TEST.yaml"),
        )
        assert results[0].status == EnumEvidenceCheckStatus.VERIFIED

    def test_command_check_fails(self, tmp_path: Path) -> None:
        """Command check with non-zero exit -> FAILED."""
        _write_contract(
            tmp_path,
            dod_evidence=[
                {
                    "id": "dod-001",
                    "description": "False check",
                    "checks": [{"check_type": "command", "command": "false"}],
                }
            ],
        )
        collector = EvidenceCollector()
        results = collector.collect(
            "OMN-TEST",
            contract_path=str(tmp_path / "OMN-TEST.yaml"),
        )
        assert results[0].status == EnumEvidenceCheckStatus.FAILED

    def test_unsupported_check_type(self, tmp_path: Path) -> None:
        """Unknown check_type -> FAILED (not SKIPPED — SKIPPED was the OMN-9571 silent-pass bug)."""
        _write_contract(
            tmp_path,
            dod_evidence=[
                {
                    "id": "dod-001",
                    "description": "Mystery check",
                    "checks": [{"check_type": "telepathy"}],
                }
            ],
        )
        collector = EvidenceCollector()
        results = collector.collect(
            "OMN-TEST",
            contract_path=str(tmp_path / "OMN-TEST.yaml"),
        )
        assert results[0].status == EnumEvidenceCheckStatus.FAILED
        assert "telepathy" in (results[0].message or "")

    def test_no_checks_defined(self, tmp_path: Path) -> None:
        """Evidence item with empty checks list -> SKIPPED."""
        _write_contract(
            tmp_path,
            dod_evidence=[
                {
                    "id": "dod-001",
                    "description": "No checks",
                    "checks": [],
                }
            ],
        )
        collector = EvidenceCollector()
        results = collector.collect(
            "OMN-TEST",
            contract_path=str(tmp_path / "OMN-TEST.yaml"),
        )
        assert results[0].status == EnumEvidenceCheckStatus.SKIPPED

    def test_ticket_id_mismatch(self, tmp_path: Path) -> None:
        """Collector rejects contract whose ticket_id doesn't match."""
        _write_contract(
            tmp_path,
            ticket_id="OMN-OTHER",
            dod_evidence=[
                {
                    "id": "dod-001",
                    "description": "True check",
                    "checks": [{"check_type": "command", "command": "true"}],
                }
            ],
        )
        collector = EvidenceCollector()
        results = collector.collect(
            "OMN-WRONG",
            contract_path=str(tmp_path / "OMN-OTHER.yaml"),
        )
        assert len(results) == 1
        assert results[0].status == EnumEvidenceCheckStatus.FAILED
        assert "mismatch" in (results[0].description or "").lower()

    def test_malformed_dod_evidence_not_list(self, tmp_path: Path) -> None:
        """dod_evidence that isn't a list -> FAILED."""
        contract = {
            "schema_version": "1.0.0",
            "ticket_id": "OMN-TEST",
            "dod_evidence": {"bad": True},
        }
        p = tmp_path / "OMN-TEST.yaml"
        p.write_text(yaml.dump(contract), encoding="utf-8")
        collector = EvidenceCollector()
        results = collector.collect(
            "OMN-TEST",
            contract_path=str(p),
        )
        assert len(results) == 1
        assert results[0].status == EnumEvidenceCheckStatus.FAILED
        assert "must be a list" in (results[0].message or "").lower()

    def test_malformed_checks_not_list(self, tmp_path: Path) -> None:
        """checks that isn't a list -> FAILED for that evidence item."""
        _write_contract(
            tmp_path,
            dod_evidence=[
                {
                    "id": "dod-001",
                    "description": "Bad checks",
                    "checks": "not a list",
                }
            ],
        )
        collector = EvidenceCollector()
        results = collector.collect(
            "OMN-TEST",
            contract_path=str(tmp_path / "OMN-TEST.yaml"),
        )
        assert len(results) == 1
        assert results[0].status == EnumEvidenceCheckStatus.FAILED
        assert "must be a list" in (results[0].message or "").lower()

    def test_shell_injection_in_ticket_id(self, tmp_path: Path) -> None:
        """Malicious ticket_id is quoted by shlex.quote and does not alter shell flow.

        The contract ticket_id matches the injected value so the test reaches the
        command-templating path. shlex.quote wraps the shell metacharacters in single
        quotes, turning them into a literal string argument for echo rather than
        executing the injected payload.  The command exits 0 and the result is
        VERIFIED, confirming that the injection was neutralised rather than executed.
        """
        malicious_ticket_id = "OMN-TEST; false"
        contract_path = _write_contract(
            tmp_path,
            ticket_id=malicious_ticket_id,
            dod_evidence=[
                {
                    "id": "dod-001",
                    "description": "Quoted substitution",
                    "checks": [
                        {"check_type": "command", "command": "echo {ticket_id}"}
                    ],
                }
            ],
        )
        collector = EvidenceCollector()
        results = collector.collect(
            malicious_ticket_id,
            contract_path=str(contract_path),
        )
        # shlex.quote turns "OMN-TEST; false" into a single-quoted shell literal so
        # the semicolon is NOT interpreted as a command separator.  echo exits 0.
        assert len(results) == 1
        assert results[0].status == EnumEvidenceCheckStatus.VERIFIED

    def test_file_exists_pass(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """file_exists check returns VERIFIED when path resolves to an existing file."""
        target = tmp_path / "artifact.txt"
        target.write_text("ok", encoding="utf-8")
        monkeypatch.setenv("OMNI_HOME", str(tmp_path))
        _write_contract(
            tmp_path,
            dod_evidence=[
                {
                    "id": "dod-001",
                    "description": "Artifact present",
                    "checks": [{"check_type": "file_exists", "path": "artifact.txt"}],
                }
            ],
        )
        collector = EvidenceCollector()
        results = collector.collect(
            "OMN-TEST",
            contract_path=str(tmp_path / "OMN-TEST.yaml"),
        )
        assert results[0].status == EnumEvidenceCheckStatus.VERIFIED

    def test_file_exists_fail(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """file_exists check returns FAILED when target is missing."""
        monkeypatch.setenv("OMNI_HOME", str(tmp_path))
        _write_contract(
            tmp_path,
            dod_evidence=[
                {
                    "id": "dod-001",
                    "description": "Missing artifact",
                    "checks": [{"check_type": "file_exists", "path": "missing.txt"}],
                }
            ],
        )
        collector = EvidenceCollector()
        results = collector.collect(
            "OMN-TEST",
            contract_path=str(tmp_path / "OMN-TEST.yaml"),
        )
        assert results[0].status == EnumEvidenceCheckStatus.FAILED
        assert "does not exist" in (results[0].message or "").lower()

    def test_file_exists_glob_pass(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Glob pattern matches at least one file -> VERIFIED."""
        (tmp_path / "report-1.md").write_text("a", encoding="utf-8")
        (tmp_path / "report-2.md").write_text("b", encoding="utf-8")
        monkeypatch.setenv("OMNI_HOME", str(tmp_path))
        _write_contract(
            tmp_path,
            dod_evidence=[
                {
                    "id": "dod-001",
                    "description": "Glob match",
                    "checks": [{"check_type": "file_exists", "path": "report-*.md"}],
                }
            ],
        )
        collector = EvidenceCollector()
        results = collector.collect(
            "OMN-TEST",
            contract_path=str(tmp_path / "OMN-TEST.yaml"),
        )
        assert results[0].status == EnumEvidenceCheckStatus.VERIFIED
        assert "match" in (results[0].message or "").lower()

    def test_file_exists_glob_fail(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Glob pattern with zero matches -> FAILED."""
        monkeypatch.setenv("OMNI_HOME", str(tmp_path))
        _write_contract(
            tmp_path,
            dod_evidence=[
                {
                    "id": "dod-001",
                    "description": "Glob empty",
                    "checks": [{"check_type": "file_exists", "path": "nope-*.md"}],
                }
            ],
        )
        collector = EvidenceCollector()
        results = collector.collect(
            "OMN-TEST",
            contract_path=str(tmp_path / "OMN-TEST.yaml"),
        )
        assert results[0].status == EnumEvidenceCheckStatus.FAILED
        assert "no matches" in (results[0].message or "").lower()

    def test_file_exists_path_traversal_rejected(self, tmp_path: Path) -> None:
        """Paths containing .. segments are rejected regardless of existence."""
        _write_contract(
            tmp_path,
            dod_evidence=[
                {
                    "id": "dod-001",
                    "description": "Traversal attempt",
                    "checks": [
                        {"check_type": "file_exists", "path": "../../etc/passwd"}
                    ],
                }
            ],
        )
        collector = EvidenceCollector()
        results = collector.collect(
            "OMN-TEST",
            contract_path=str(tmp_path / "OMN-TEST.yaml"),
        )
        assert results[0].status == EnumEvidenceCheckStatus.FAILED
        assert "traversal" in (results[0].message or "").lower()

    def test_file_exists_absolute_path_outside_base_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Absolute paths outside OMNI_HOME are rejected as containment violations."""
        monkeypatch.setenv("OMNI_HOME", str(tmp_path))
        # Create a real file in a sibling dir so we prove containment, not just non-existence.
        outside = tmp_path.parent / "outside.txt"
        outside.write_text("nope", encoding="utf-8")
        _write_contract(
            tmp_path,
            dod_evidence=[
                {
                    "id": "dod-001",
                    "description": "Absolute outside base",
                    "checks": [{"check_type": "file_exists", "path": str(outside)}],
                }
            ],
        )
        collector = EvidenceCollector()
        results = collector.collect(
            "OMN-TEST",
            contract_path=str(tmp_path / "OMN-TEST.yaml"),
        )
        assert results[0].status == EnumEvidenceCheckStatus.FAILED
        assert "traversal" in (results[0].message or "").lower()

    def test_file_exists_symlink_escape_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Symlinks pointing outside OMNI_HOME are rejected after resolve()."""
        outside = tmp_path.parent / "outside-symlink-target.txt"
        outside.write_text("outside", encoding="utf-8")
        inside = tmp_path / "link-to-outside"
        inside.symlink_to(outside)
        monkeypatch.setenv("OMNI_HOME", str(tmp_path))
        _write_contract(
            tmp_path,
            dod_evidence=[
                {
                    "id": "dod-001",
                    "description": "Symlink escape",
                    "checks": [
                        {"check_type": "file_exists", "path": "link-to-outside"}
                    ],
                }
            ],
        )
        collector = EvidenceCollector()
        results = collector.collect(
            "OMN-TEST",
            contract_path=str(tmp_path / "OMN-TEST.yaml"),
        )
        assert results[0].status == EnumEvidenceCheckStatus.FAILED
        assert "traversal" in (results[0].message or "").lower()

    def test_file_exists_glob_symlink_escape_filtered(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Glob matches that resolve outside the base are filtered out."""
        outside = tmp_path.parent / "glob-outside.md"
        outside.write_text("outside", encoding="utf-8")
        (tmp_path / "link-outside.md").symlink_to(outside)
        monkeypatch.setenv("OMNI_HOME", str(tmp_path))
        _write_contract(
            tmp_path,
            dod_evidence=[
                {
                    "id": "dod-001",
                    "description": "Glob symlink escape",
                    "checks": [{"check_type": "file_exists", "path": "link-*.md"}],
                }
            ],
        )
        collector = EvidenceCollector()
        results = collector.collect(
            "OMN-TEST",
            contract_path=str(tmp_path / "OMN-TEST.yaml"),
        )
        assert results[0].status == EnumEvidenceCheckStatus.FAILED
        assert "no matches" in (results[0].message or "").lower()

    def test_file_exists_absolute_path_inside_base_allowed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Absolute paths that resolve inside OMNI_HOME are permitted."""
        target = tmp_path / "inside.txt"
        target.write_text("ok", encoding="utf-8")
        monkeypatch.setenv("OMNI_HOME", str(tmp_path))
        _write_contract(
            tmp_path,
            dod_evidence=[
                {
                    "id": "dod-001",
                    "description": "Absolute inside base",
                    "checks": [{"check_type": "file_exists", "path": str(target)}],
                }
            ],
        )
        collector = EvidenceCollector()
        results = collector.collect(
            "OMN-TEST",
            contract_path=str(tmp_path / "OMN-TEST.yaml"),
        )
        assert results[0].status == EnumEvidenceCheckStatus.VERIFIED

    def test_file_exists_empty_path_rejected(self, tmp_path: Path) -> None:
        """Missing path field -> FAILED."""
        _write_contract(
            tmp_path,
            dod_evidence=[
                {
                    "id": "dod-001",
                    "description": "No path",
                    "checks": [{"check_type": "file_exists"}],
                }
            ],
        )
        collector = EvidenceCollector()
        results = collector.collect(
            "OMN-TEST",
            contract_path=str(tmp_path / "OMN-TEST.yaml"),
        )
        assert results[0].status == EnumEvidenceCheckStatus.FAILED
        assert "empty path" in (results[0].message or "").lower()

    def test_multiple_evidence_items(self, tmp_path: Path) -> None:
        """Multiple dod_evidence items produce multiple results."""
        _write_contract(
            tmp_path,
            dod_evidence=[
                {
                    "id": "dod-001",
                    "description": "Pass",
                    "checks": [{"check_type": "command", "command": "true"}],
                },
                {
                    "id": "dod-002",
                    "description": "Fail",
                    "checks": [{"check_type": "command", "command": "false"}],
                },
            ],
        )
        collector = EvidenceCollector()
        results = collector.collect(
            "OMN-TEST",
            contract_path=str(tmp_path / "OMN-TEST.yaml"),
        )
        assert len(results) == 2
        assert results[0].status == EnumEvidenceCheckStatus.VERIFIED
        assert results[1].status == EnumEvidenceCheckStatus.FAILED

    def test_test_passes_check_type_pass(self, tmp_path: Path) -> None:
        """check_type: test_passes runs the command and VERIFIES on exit code 0.

        Regression for OMN-10046 — previously test_passes fell through to the
        unknown-check-type branch and FAILED with "Supported: command,
        file_exists.", blocking 5 legitimately-Done tickets.
        """
        _write_contract(
            tmp_path,
            dod_evidence=[
                {
                    "id": "dod-001",
                    "description": "Pytest exits 0",
                    "checks": [
                        {
                            "check_type": "test_passes",
                            "check_value": "true",
                        }
                    ],
                }
            ],
        )
        collector = EvidenceCollector()
        results = collector.collect(
            "OMN-TEST",
            contract_path=str(tmp_path / "OMN-TEST.yaml"),
        )
        assert results[0].status == EnumEvidenceCheckStatus.VERIFIED
        # Ensure the message reflects test_passes execution, not the unknown-type path.
        assert "Unknown check_type" not in (results[0].message or "")

    def test_test_passes_check_type_fail(self, tmp_path: Path) -> None:
        """check_type: test_passes FAILS when the command exits non-zero."""
        _write_contract(
            tmp_path,
            dod_evidence=[
                {
                    "id": "dod-001",
                    "description": "Pytest exits 1",
                    "checks": [
                        {
                            "check_type": "test_passes",
                            "check_value": "false",
                        }
                    ],
                }
            ],
        )
        collector = EvidenceCollector()
        results = collector.collect(
            "OMN-TEST",
            contract_path=str(tmp_path / "OMN-TEST.yaml"),
        )
        assert results[0].status == EnumEvidenceCheckStatus.FAILED
        assert "Unknown check_type" not in (results[0].message or "")

    def test_test_passes_accepts_command_field(self, tmp_path: Path) -> None:
        """test_passes accepts `command` as well as `check_value` for parity with command checks."""
        _write_contract(
            tmp_path,
            dod_evidence=[
                {
                    "id": "dod-001",
                    "description": "Pytest via command field",
                    "checks": [
                        {
                            "check_type": "test_passes",
                            "command": "true",
                        }
                    ],
                }
            ],
        )
        collector = EvidenceCollector()
        results = collector.collect(
            "OMN-TEST",
            contract_path=str(tmp_path / "OMN-TEST.yaml"),
        )
        assert results[0].status == EnumEvidenceCheckStatus.VERIFIED

    def test_test_passes_empty_command_fails(self, tmp_path: Path) -> None:
        """test_passes with no command/check_value FAILS (cannot silently pass)."""
        _write_contract(
            tmp_path,
            dod_evidence=[
                {
                    "id": "dod-001",
                    "description": "No command",
                    "checks": [
                        {
                            "check_type": "test_passes",
                        }
                    ],
                }
            ],
        )
        collector = EvidenceCollector()
        results = collector.collect(
            "OMN-TEST",
            contract_path=str(tmp_path / "OMN-TEST.yaml"),
        )
        assert results[0].status == EnumEvidenceCheckStatus.FAILED


@pytest.mark.unit
class TestEvidenceCollectorCwd:
    """OMN-10078: ``cwd`` field on a command/test_passes check.

    Replaces the brittle ``cd ${OMNI_HOME}/<repo> && `` shell-prefix fix that
    PR #448 (OMN-10049) introduced. The runner must:

    - default to inherited cwd when the field is absent (backwards compat)
    - expand ``${OMNI_HOME}``, ``${PR_NUMBER}``, ``${REPO}``, ``${TICKET_ID}``
      template tokens before resolution
    - reject ``..`` segments and paths that escape ``OMNI_HOME`` after symlink
      resolution
    - actually pass ``cwd=`` to subprocess.run (proven via ``pwd`` stdout)
    """

    def test_cwd_absent_inherits_caller_cwd(self, tmp_path: Path) -> None:
        """When cwd is omitted, behaviour matches the legacy inherited-cwd path."""
        _write_contract(
            tmp_path,
            dod_evidence=[
                {
                    "id": "dod-001",
                    "description": "No cwd declared",
                    "checks": [{"check_type": "command", "command": "true"}],
                }
            ],
        )
        collector = EvidenceCollector()
        results = collector.collect(
            "OMN-TEST",
            contract_path=str(tmp_path / "OMN-TEST.yaml"),
        )
        assert results[0].status == EnumEvidenceCheckStatus.VERIFIED

    @pytest.mark.parametrize("nested", [False, True])
    @pytest.mark.parametrize("command", ["pwd", "false"])
    def test_cwd_own_repository_unchanged_by_replay_setup(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        nested: bool,
        command: str,
    ) -> None:
        """New replay setup preserves own-tree cwd success and failure results."""
        from omnimarket.nodes.node_dod_verify.handlers import (
            handler_occ_replay as replay,
        )

        home = tmp_path / "registry"
        tree = home / "omnimarket"
        contracts = tree / "contracts"
        contracts.mkdir(parents=True)
        target = tree / "nested" if nested else tree
        target.mkdir(exist_ok=True)
        marker = target / "product.txt"
        marker.write_text("own product\n")
        original_modes = target.stat().st_mode, marker.stat().st_mode
        monkeypatch.setenv("OMNI_HOME", str(home))
        contract = _write_contract(
            contracts,
            dod_evidence=[
                {
                    "id": "own-cwd",
                    "description": "Own repository cwd",
                    "checks": [
                        {
                            "check_type": "command",
                            "check_value": command,
                            "cwd": "${OMNI_HOME}/omnimarket"
                            + ("/nested" if nested else ""),
                        }
                    ],
                }
            ],
        )

        def unexpected_clone(repository: str) -> None:
            pytest.fail(f"own repository must not be provisioned: {repository}")

        monkeypatch.setattr(replay, "canonical_clone", unexpected_clone)
        collector = EvidenceCollector()
        before = collector.collect("OMN-TEST", contract_path=str(contract))[0]
        expected = (
            EnumEvidenceCheckStatus.VERIFIED
            if command == "pwd"
            else EnumEvidenceCheckStatus.FAILED
        )
        assert before.status == expected
        replay.HandlerOccReplay._prepare_sibling_checkouts(
            "OmniNode-ai", tree, home, [contract], "2026-10-12T00:00:00Z"
        )
        after = collector.collect("OMN-TEST", contract_path=str(contract))[0]
        assert after.status == before.status
        assert after.unverifiable_cause == before.unverifiable_cause
        assert re.sub(r"\(\d+ms\)", "(elapsed)", after.message or "") == re.sub(
            r"\(\d+ms\)", "(elapsed)", before.message or ""
        )
        if command == "pwd":
            assert str(target.resolve()) in (after.message or "")
        assert marker.read_text() == "own product\n"
        assert (target.stat().st_mode, marker.stat().st_mode) == original_modes
        assert sorted(path.name for path in home.iterdir()) == [tree.name]

    def test_cwd_runs_command_in_specified_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A literal cwd is passed to subprocess.run and pwd reflects it."""
        target = tmp_path / "subdir"
        target.mkdir()
        monkeypatch.setenv("OMNI_HOME", str(tmp_path))
        _write_contract(
            tmp_path,
            dod_evidence=[
                {
                    "id": "dod-001",
                    "description": "Runs in subdir",
                    "checks": [
                        {
                            "check_type": "command",
                            "command": "pwd",
                            "cwd": str(target),
                        }
                    ],
                }
            ],
        )
        collector = EvidenceCollector()
        results = collector.collect(
            "OMN-TEST",
            contract_path=str(tmp_path / "OMN-TEST.yaml"),
        )
        assert results[0].status == EnumEvidenceCheckStatus.VERIFIED
        # ``pwd`` resolves the cwd target; the receipt message includes the
        # truncated stdout via the OK message format.
        # Use Path.resolve() because macOS routes /private/var symlinks.
        expected = str(Path(target).resolve())
        assert expected in (results[0].message or "")

    def test_cwd_omni_home_template_expanded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``${OMNI_HOME}`` in cwd is substituted before resolution."""
        sub = tmp_path / "repo"
        sub.mkdir()
        monkeypatch.setenv("OMNI_HOME", str(tmp_path))
        _write_contract(
            tmp_path,
            dod_evidence=[
                {
                    "id": "dod-001",
                    "description": "OMNI_HOME templated cwd",
                    "checks": [
                        {
                            "check_type": "command",
                            "command": "pwd",
                            "cwd": "${OMNI_HOME}/repo",
                        }
                    ],
                }
            ],
        )
        collector = EvidenceCollector()
        results = collector.collect(
            "OMN-TEST",
            contract_path=str(tmp_path / "OMN-TEST.yaml"),
        )
        assert results[0].status == EnumEvidenceCheckStatus.VERIFIED
        assert str(Path(sub).resolve()) in (results[0].message or "")

    def test_cwd_ticket_id_token_expanded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``${TICKET_ID}`` is substituted with the active ticket id."""
        sub = tmp_path / "OMN-TEST"
        sub.mkdir()
        monkeypatch.setenv("OMNI_HOME", str(tmp_path))
        _write_contract(
            tmp_path,
            dod_evidence=[
                {
                    "id": "dod-001",
                    "description": "TICKET_ID templated cwd",
                    "checks": [
                        {
                            "check_type": "command",
                            "command": "pwd",
                            "cwd": "${OMNI_HOME}/${TICKET_ID}",
                        }
                    ],
                }
            ],
        )
        collector = EvidenceCollector()
        results = collector.collect(
            "OMN-TEST",
            contract_path=str(tmp_path / "OMN-TEST.yaml"),
        )
        assert results[0].status == EnumEvidenceCheckStatus.VERIFIED
        assert str(Path(sub).resolve()) in (results[0].message or "")

    def test_cwd_pr_number_and_repo_tokens_expanded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Mirrors OMN-10086: PR_NUMBER and REPO env vars feed cwd substitution."""
        sub = tmp_path / "456" / "OmniNode-ai" / "omnibase_core"
        sub.mkdir(parents=True)
        monkeypatch.setenv("OMNI_HOME", str(tmp_path))
        monkeypatch.setenv("PR_NUMBER", "456")
        monkeypatch.setenv("REPO", "OmniNode-ai/omnibase_core")
        _write_contract(
            tmp_path,
            dod_evidence=[
                {
                    "id": "dod-001",
                    "description": "PR_NUMBER + REPO templated cwd",
                    "checks": [
                        {
                            "check_type": "command",
                            "command": "pwd",
                            "cwd": "${OMNI_HOME}/${PR_NUMBER}/${REPO}",
                        }
                    ],
                }
            ],
        )
        collector = EvidenceCollector()
        results = collector.collect(
            "OMN-TEST",
            contract_path=str(tmp_path / "OMN-TEST.yaml"),
        )
        assert results[0].status == EnumEvidenceCheckStatus.VERIFIED
        assert str(Path(sub).resolve()) in (results[0].message or "")

    def test_cwd_traversal_segments_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``..`` segments in raw cwd are rejected before any substitution."""
        monkeypatch.setenv("OMNI_HOME", str(tmp_path))
        _write_contract(
            tmp_path,
            dod_evidence=[
                {
                    "id": "dod-001",
                    "description": "Traversal cwd",
                    "checks": [
                        {
                            "check_type": "command",
                            "command": "pwd",
                            "cwd": "${OMNI_HOME}/../etc",
                        }
                    ],
                }
            ],
        )
        collector = EvidenceCollector()
        results = collector.collect(
            "OMN-TEST",
            contract_path=str(tmp_path / "OMN-TEST.yaml"),
        )
        assert results[0].status == EnumEvidenceCheckStatus.FAILED
        assert "traversal" in (results[0].message or "").lower()

    def test_cwd_outside_omni_home_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Resolved cwd outside OMNI_HOME is rejected as a containment violation."""
        outside = tmp_path.parent / "outside-cwd"
        outside.mkdir(exist_ok=True)
        monkeypatch.setenv("OMNI_HOME", str(tmp_path))
        _write_contract(
            tmp_path,
            dod_evidence=[
                {
                    "id": "dod-001",
                    "description": "Absolute cwd outside base",
                    "checks": [
                        {
                            "check_type": "command",
                            "command": "pwd",
                            "cwd": str(outside),
                        }
                    ],
                }
            ],
        )
        collector = EvidenceCollector()
        results = collector.collect(
            "OMN-TEST",
            contract_path=str(tmp_path / "OMN-TEST.yaml"),
        )
        assert results[0].status == EnumEvidenceCheckStatus.FAILED
        assert (
            "containment" in (results[0].message or "").lower()
            or "escapes" in (results[0].message or "").lower()
        )

    def test_cwd_unresolved_template_token_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An undefined env-var template (e.g. ${REPO} when REPO is unset) fails fast."""
        monkeypatch.setenv("OMNI_HOME", str(tmp_path))
        monkeypatch.delenv("REPO", raising=False)
        monkeypatch.delenv("PR_NUMBER", raising=False)
        _write_contract(
            tmp_path,
            dod_evidence=[
                {
                    "id": "dod-001",
                    "description": "Unset REPO token",
                    "checks": [
                        {
                            "check_type": "command",
                            "command": "pwd",
                            "cwd": "${OMNI_HOME}/${REPO_UNSET_TOKEN}",
                        }
                    ],
                }
            ],
        )
        collector = EvidenceCollector()
        results = collector.collect(
            "OMN-TEST",
            contract_path=str(tmp_path / "OMN-TEST.yaml"),
        )
        # ${REPO_UNSET_TOKEN} is not in the substitution table and is not a
        # set env var; expandvars leaves it literal => unresolved-token path.
        assert results[0].status == EnumEvidenceCheckStatus.FAILED
        msg = (results[0].message or "").lower()
        assert "unresolved" in msg or "does not exist" in msg

    def test_cwd_missing_directory_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A cwd that resolves to a non-existent path fails with a clear message."""
        monkeypatch.setenv("OMNI_HOME", str(tmp_path))
        _write_contract(
            tmp_path,
            dod_evidence=[
                {
                    "id": "dod-001",
                    "description": "Missing cwd dir",
                    "checks": [
                        {
                            "check_type": "command",
                            "command": "pwd",
                            "cwd": "${OMNI_HOME}/nope-does-not-exist",
                        }
                    ],
                }
            ],
        )
        collector = EvidenceCollector()
        results = collector.collect(
            "OMN-TEST",
            contract_path=str(tmp_path / "OMN-TEST.yaml"),
        )
        assert results[0].status == EnumEvidenceCheckStatus.FAILED
        assert "does not exist" in (results[0].message or "").lower()

    def test_cwd_non_string_value_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A cwd that isn't a string (e.g. dict) fails with a type error."""
        monkeypatch.setenv("OMNI_HOME", str(tmp_path))
        _write_contract(
            tmp_path,
            dod_evidence=[
                {
                    "id": "dod-001",
                    "description": "cwd as dict",
                    "checks": [
                        {
                            "check_type": "command",
                            "command": "pwd",
                            "cwd": {"not": "a string"},
                        }
                    ],
                }
            ],
        )
        collector = EvidenceCollector()
        results = collector.collect(
            "OMN-TEST",
            contract_path=str(tmp_path / "OMN-TEST.yaml"),
        )
        assert results[0].status == EnumEvidenceCheckStatus.FAILED
        assert "must be a string" in (results[0].message or "").lower()

    def test_cwd_works_for_test_passes_check_type(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """test_passes shares the command runner; cwd applies to it too."""
        sub = tmp_path / "tests-dir"
        sub.mkdir()
        monkeypatch.setenv("OMNI_HOME", str(tmp_path))
        _write_contract(
            tmp_path,
            dod_evidence=[
                {
                    "id": "dod-001",
                    "description": "test_passes with cwd",
                    "checks": [
                        {
                            "check_type": "test_passes",
                            "check_value": "pwd",
                            "cwd": str(sub),
                        }
                    ],
                }
            ],
        )
        collector = EvidenceCollector()
        results = collector.collect(
            "OMN-TEST",
            contract_path=str(tmp_path / "OMN-TEST.yaml"),
        )
        assert results[0].status == EnumEvidenceCheckStatus.VERIFIED
        assert str(Path(sub).resolve()) in (results[0].message or "")


@pytest.mark.unit
class TestPlaceholderSubstitution:
    """OMN-10476: check_value placeholder substitution before probe execution."""

    def test_ticket_id_shell_style_substituted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """${TICKET_ID} in check_value is substituted with the active ticket id."""
        monkeypatch.setenv("OMNI_HOME", str(tmp_path))
        _write_contract(
            tmp_path,
            dod_evidence=[
                {
                    "id": "dod-001",
                    "description": "TICKET_ID substitution",
                    "checks": [
                        {
                            "check_type": "command",
                            "check_value": "echo ${TICKET_ID}",
                        }
                    ],
                }
            ],
        )
        collector = EvidenceCollector()
        results = collector.collect(
            "OMN-TEST",
            contract_path=str(tmp_path / "OMN-TEST.yaml"),
        )
        assert results[0].status == EnumEvidenceCheckStatus.VERIFIED
        assert "OMN-TEST" in (results[0].message or "")

    def test_pr_and_repo_python_style_substituted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """{pr} and {repo} placeholders are substituted from env vars."""
        monkeypatch.setenv("OMNI_HOME", str(tmp_path))
        monkeypatch.setenv("PR_NUMBER", "42")
        monkeypatch.setenv("REPO", "OmniNode-ai/omnibase_core")
        _write_contract(
            tmp_path,
            dod_evidence=[
                {
                    "id": "dod-001",
                    "description": "pr and repo substitution",
                    "checks": [
                        {
                            "check_type": "command",
                            "check_value": "echo {pr} {repo}",
                        }
                    ],
                }
            ],
        )
        collector = EvidenceCollector()
        results = collector.collect(
            "OMN-TEST",
            contract_path=str(tmp_path / "OMN-TEST.yaml"),
        )
        assert results[0].status == EnumEvidenceCheckStatus.VERIFIED
        msg = results[0].message or ""
        assert "42" in msg
        assert "omnibase_core" in msg

    def test_pr_shell_style_substituted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """${PR_NUMBER} and ${REPO} shell-style placeholders are substituted."""
        monkeypatch.setenv("OMNI_HOME", str(tmp_path))
        monkeypatch.setenv("PR_NUMBER", "99")
        monkeypatch.setenv("REPO", "OmniNode-ai/omnimarket")
        _write_contract(
            tmp_path,
            dod_evidence=[
                {
                    "id": "dod-001",
                    "description": "shell-style PR and REPO",
                    "checks": [
                        {
                            "check_type": "command",
                            "check_value": "echo ${PR_NUMBER} ${REPO}",
                        }
                    ],
                }
            ],
        )
        collector = EvidenceCollector()
        results = collector.collect(
            "OMN-TEST",
            contract_path=str(tmp_path / "OMN-TEST.yaml"),
        )
        assert results[0].status == EnumEvidenceCheckStatus.VERIFIED
        msg = results[0].message or ""
        assert "99" in msg
        assert "omnimarket" in msg

    def test_missing_pr_number_errors_gracefully(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When {pr} is in command but PR_NUMBER is unset and gh finds nothing, FAIL with clear message."""
        monkeypatch.setenv("OMNI_HOME", str(tmp_path))
        monkeypatch.delenv("PR_NUMBER", raising=False)
        monkeypatch.delenv("REPO", raising=False)
        _write_contract(
            tmp_path,
            dod_evidence=[
                {
                    "id": "dod-001",
                    "description": "unresolvable pr placeholder",
                    "checks": [
                        {
                            "check_type": "command",
                            # Use a ticket id that won't have a real merged PR in any local gh context
                            "check_value": "gh pr view {pr} --repo OmniNode-ai/omnimarket --json state",
                        }
                    ],
                }
            ],
        )
        collector = EvidenceCollector()

        # Patch _lookup_pr_for_ticket to return empty (simulates no merged PR found)
        def _no_pr(ticket_id: str) -> str:
            return ""

        collector._lookup_pr_for_ticket = _no_pr  # type: ignore[method-assign]

        results = collector.collect(
            "OMN-TEST",
            contract_path=str(tmp_path / "OMN-TEST.yaml"),
        )
        assert results[0].status == EnumEvidenceCheckStatus.FAILED
        msg = (results[0].message or "").lower()
        assert "pr number" in msg or "cannot resolve" in msg

    def test_occ_contract_injects_occ_cwd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Commands from OCC contracts run with cwd set to onex_change_control repo."""
        occ_dir = tmp_path / "onex_change_control"
        occ_dir.mkdir()
        contracts_dir = occ_dir / "contracts"
        contracts_dir.mkdir()
        monkeypatch.setenv("OMNI_HOME", str(tmp_path))

        # Write contract inside OCC contracts dir
        contract = {
            "schema_version": "1.0.0",
            "ticket_id": "OMN-TEST",
            "dod_evidence": [
                {
                    "id": "dod-001",
                    "description": "pwd reflects OCC dir",
                    "checks": [{"check_type": "command", "command": "pwd"}],
                }
            ],
        }
        contract_path = contracts_dir / "OMN-TEST.yaml"
        import yaml

        contract_path.write_text(yaml.dump(contract), encoding="utf-8")

        collector = EvidenceCollector()
        results = collector.collect(
            "OMN-TEST",
            contract_path=str(contract_path),
        )
        assert results[0].status == EnumEvidenceCheckStatus.VERIFIED
        expected_cwd = str(Path(occ_dir).resolve())
        assert expected_cwd in (results[0].message or "")

    def test_non_occ_contract_does_not_inject_occ_cwd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Commands from non-OCC contracts do NOT get occ cwd injection."""
        # Set up an OCC dir to prove cwd injection is NOT used when contract is elsewhere
        occ_dir = tmp_path / "onex_change_control"
        occ_dir.mkdir()
        monkeypatch.setenv("OMNI_HOME", str(tmp_path))

        # Contract lives outside onex_change_control
        _write_contract(
            tmp_path,
            dod_evidence=[
                {
                    "id": "dod-001",
                    "description": "pwd does not point to OCC",
                    "checks": [{"check_type": "command", "command": "pwd"}],
                }
            ],
        )
        collector = EvidenceCollector()
        results = collector.collect(
            "OMN-TEST",
            contract_path=str(tmp_path / "OMN-TEST.yaml"),
        )
        assert results[0].status == EnumEvidenceCheckStatus.VERIFIED
        # cwd should NOT be occ_dir
        occ_cwd = str(Path(occ_dir).resolve())
        assert occ_cwd not in (results[0].message or "")


@pytest.mark.unit
class TestFileExistsOccCwd:
    """OMN-10542: file_exists must use the same OCC cwd inference as command checks.

    OCC contracts under ``onex_change_control/contracts/`` use relative paths
    like ``drift/dod_receipts/<ticket>/...`` that are anchored at the OCC repo
    root, not at OMNI_HOME. Before this fix, ``_run_file_exists_check``
    resolved relative paths against OMNI_HOME directly, false-failing every
    OCC ``file_exists`` check on real receipt files.
    """

    @staticmethod
    def _write_occ_contract(
        occ_contracts_dir: Path,
        ticket_id: str,
        dod_evidence: list[dict],
    ) -> Path:
        """Write a contract under an onex_change_control/contracts/ tree."""
        contract = {
            "schema_version": "1.0.0",
            "ticket_id": ticket_id,
            "dod_evidence": dod_evidence,
        }
        path = occ_contracts_dir / f"{ticket_id}.yaml"
        path.write_text(yaml.dump(contract), encoding="utf-8")
        return path

    def test_occ_relative_file_exists_resolves_against_occ_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OCC contract: relative file_exists path resolves against OCC repo, not OMNI_HOME."""
        occ_dir = tmp_path / "onex_change_control"
        occ_contracts = occ_dir / "contracts"
        occ_contracts.mkdir(parents=True)
        # Receipt file lives under the OCC repo, anchored at OCC root.
        receipt_dir = occ_dir / "drift" / "dod_receipts" / "OMN-9788" / "dod-001"
        receipt_dir.mkdir(parents=True)
        receipt = receipt_dir / "file_exists.yaml"
        receipt.write_text("status: PASS\n", encoding="utf-8")
        monkeypatch.setenv("OMNI_HOME", str(tmp_path))

        contract_path = self._write_occ_contract(
            occ_contracts,
            "OMN-9788",
            [
                {
                    "id": "dod-001",
                    "description": "OCC receipt path is relative to OCC root",
                    "checks": [
                        {
                            "check_type": "file_exists",
                            "check_value": (
                                "drift/dod_receipts/OMN-9788/dod-001/file_exists.yaml"
                            ),
                        }
                    ],
                }
            ],
        )
        collector = EvidenceCollector()
        results = collector.collect("OMN-9788", contract_path=str(contract_path))
        assert len(results) == 1
        assert results[0].status == EnumEvidenceCheckStatus.VERIFIED, (
            f"OCC file_exists should resolve against OCC root, got: {results[0].message!r}"
        )

    def test_occ_relative_file_exists_glob_resolves_against_occ_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OCC glob patterns also anchor at the OCC repo root."""
        occ_dir = tmp_path / "onex_change_control"
        occ_contracts = occ_dir / "contracts"
        occ_contracts.mkdir(parents=True)
        receipt_dir = occ_dir / "drift" / "dod_receipts" / "OMN-9788"
        (receipt_dir / "dod-001").mkdir(parents=True)
        (receipt_dir / "dod-002").mkdir(parents=True)
        (receipt_dir / "dod-001" / "file_exists.yaml").write_text("a", encoding="utf-8")
        (receipt_dir / "dod-002" / "file_exists.yaml").write_text("b", encoding="utf-8")
        monkeypatch.setenv("OMNI_HOME", str(tmp_path))

        contract_path = self._write_occ_contract(
            occ_contracts,
            "OMN-9788",
            [
                {
                    "id": "dod-001",
                    "description": "Glob anchored at OCC root",
                    "checks": [
                        {
                            "check_type": "file_exists",
                            "check_value": "drift/dod_receipts/OMN-9788/*/file_exists.yaml",
                        }
                    ],
                }
            ],
        )
        collector = EvidenceCollector()
        results = collector.collect("OMN-9788", contract_path=str(contract_path))
        assert len(results) == 1
        assert results[0].status == EnumEvidenceCheckStatus.VERIFIED
        assert "2 match" in (results[0].message or "")

    def test_non_occ_contract_relative_file_exists_resolves_against_omni_home(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Non-OCC contract: relative file_exists path resolves against OMNI_HOME (legacy)."""
        # Create an OCC tree to prove the OCC root is NOT used when contract lives elsewhere.
        occ_dir = tmp_path / "onex_change_control"
        (occ_dir / "drift" / "dod_receipts" / "OMN-TEST" / "dod-001").mkdir(
            parents=True
        )
        # Receipt at OMNI_HOME root that the test will resolve against.
        target = tmp_path / "artifact.yaml"
        target.write_text("ok", encoding="utf-8")
        monkeypatch.setenv("OMNI_HOME", str(tmp_path))
        _write_contract(
            tmp_path,
            dod_evidence=[
                {
                    "id": "dod-001",
                    "description": "Non-OCC contract resolves against OMNI_HOME",
                    "checks": [{"check_type": "file_exists", "path": "artifact.yaml"}],
                }
            ],
        )
        collector = EvidenceCollector()
        results = collector.collect(
            "OMN-TEST",
            contract_path=str(tmp_path / "OMN-TEST.yaml"),
        )
        assert results[0].status == EnumEvidenceCheckStatus.VERIFIED

    def test_occ_relative_file_exists_missing_still_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OCC file_exists for a missing receipt still fails — fix doesn't mask absence."""
        occ_dir = tmp_path / "onex_change_control"
        occ_contracts = occ_dir / "contracts"
        occ_contracts.mkdir(parents=True)
        monkeypatch.setenv("OMNI_HOME", str(tmp_path))

        contract_path = self._write_occ_contract(
            occ_contracts,
            "OMN-9788",
            [
                {
                    "id": "dod-001",
                    "description": "Missing OCC receipt still fails",
                    "checks": [
                        {
                            "check_type": "file_exists",
                            "check_value": (
                                "drift/dod_receipts/OMN-9788/dod-001/file_exists.yaml"
                            ),
                        }
                    ],
                }
            ],
        )
        collector = EvidenceCollector()
        results = collector.collect("OMN-9788", contract_path=str(contract_path))
        assert results[0].status == EnumEvidenceCheckStatus.FAILED
        assert "does not exist" in (results[0].message or "").lower()

    def test_occ_relative_file_exists_traversal_still_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OCC contracts cannot escape OMNI_HOME via .. segments."""
        occ_dir = tmp_path / "onex_change_control"
        occ_contracts = occ_dir / "contracts"
        occ_contracts.mkdir(parents=True)
        monkeypatch.setenv("OMNI_HOME", str(tmp_path))

        contract_path = self._write_occ_contract(
            occ_contracts,
            "OMN-9788",
            [
                {
                    "id": "dod-001",
                    "description": "Traversal attempt under OCC",
                    "checks": [
                        {
                            "check_type": "file_exists",
                            "check_value": "../../etc/passwd",
                        }
                    ],
                }
            ],
        )
        collector = EvidenceCollector()
        results = collector.collect("OMN-9788", contract_path=str(contract_path))
        assert results[0].status == EnumEvidenceCheckStatus.FAILED
        assert "traversal" in (results[0].message or "").lower()


@pytest.mark.unit
class TestHandlerWithCollector:
    """Integration tests: handler auto-collects evidence when not provided."""

    def test_handler_collects_from_contract(self, tmp_path: Path) -> None:
        """Handler with no evidence_results calls collector."""
        _write_contract(
            tmp_path,
            dod_evidence=[
                {
                    "id": "dod-001",
                    "description": "True check",
                    "checks": [{"check_type": "command", "command": "true"}],
                }
            ],
        )
        handler = HandlerDodVerify()
        result = handler.handle(
            {
                "correlation_id": str(uuid4()),
                "ticket_id": "OMN-TEST",
                "contract_path": str(tmp_path / "OMN-TEST.yaml"),
                "execution_audience": "hosted",
                "dry_run": False,
                "requested_at": datetime.now(tz=UTC).isoformat(),
            }
        )
        assert isinstance(result, dict)
        assert result["status"] == "verified"
        assert result["total_checks"] == 1
        assert result["verified_count"] == 1

    def test_handler_pre_provided_results_still_work(self, tmp_path: Path) -> None:
        """Pre-provided evidence_results bypass collector (backward compat)."""
        from omnimarket.nodes.node_dod_verify.models.model_dod_verify_start_command import (
            ModelDodVerifyStartCommand,
        )
        from omnimarket.nodes.node_dod_verify.models.model_dod_verify_state import (
            ModelEvidenceCheckResult,
        )

        handler = HandlerDodVerify()
        cmd = ModelDodVerifyStartCommand(
            correlation_id=uuid4(),
            ticket_id="OMN-TEST",
            dry_run=False,
            requested_at=datetime.now(tz=UTC),
        )
        checks = [
            ModelEvidenceCheckResult(
                evidence_id="dod-001",
                description="Pass",
                status=EnumEvidenceCheckStatus.VERIFIED,
            ),
        ]
        state = handler._handle_typed(cmd, evidence_results=checks)
        assert state.status == EnumDodVerifyStatus.VERIFIED
        assert state.verified_count == 1

    def test_handler_no_contract_failed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Handler returns FAILED when no product repository governs the ticket."""
        monkeypatch.delenv("OMNI_HOME", raising=False)
        handler = HandlerDodVerify()
        result = handler.handle(
            {
                "correlation_id": str(uuid4()),
                "ticket_id": "OMN-NOEXIST-99999",
                "execution_audience": "hosted",
                "dry_run": False,
                "requested_at": datetime.now(tz=UTC).isoformat(),
            }
        )
        assert isinstance(result, dict)
        assert result["status"] == "failed"
