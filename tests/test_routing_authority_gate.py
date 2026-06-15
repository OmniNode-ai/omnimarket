# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Tests for the routing-authority demo gate (OMN-12821, OMN-12877, OMN-12883).

The gate proves three things about the routing authority:

POSITIVE: provider, model (served_model_id), endpoint_ref, resolved endpoint,
    and route_source all resolve from contract / overlay / routing authority,
    with the source file/line recorded for each.

NEGATIVE: no demo-path source reads env vars for endpoint/provider/model, no
    hardcoded provider literals, and no fallback endpoint strings after route
    resolution. Secret-value resolution (os.environ[api_key_ref]) at the effect
    boundary is the SANCTIONED contract-native pattern and is NOT flagged.

RESIDUE (OMN-12877): scope-extended ratchet over confirmed residue files. The
    gate enforces that violation counts never INCREASE beyond baselined values.
    New violations above the baseline fail the gate.

SHAPE (OMN-12883): every bifrost backend respects the provider-class endpoint
    URL shape contract: overlay-supplied (endpoint_url_env set) → endpoint_url null;
    static-URL non-cli backends → endpoint_url is a non-null complete URL.

These tests assert the live demo path passes AND that the gate actually fails
when a violation is injected (proving it is enforcing, not a rubber stamp).

Tickets: OMN-12821, OMN-12877, OMN-12883
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

_SCRIPTS_CI = Path(__file__).parent.parent / "scripts" / "ci"
sys.path.insert(0, str(_SCRIPTS_CI))

from check_routing_authority import (  # noqa: E402
    _REQUIRED_ROUTING_KEYS,
    _RESIDUE_SOURCES,
    _is_api_key_resolution,
    _resolve_endpoint_for_ref,
    _scan_env_reads,
    _scan_literal_tokens,
    build_evidence_packet,
    build_negative_audit,
    build_positive_proof,
    build_provider_endpoint_shape_audit,
    build_residue_audit,
)

_REPO_ROOT = Path(__file__).parent.parent


@pytest.mark.unit
class TestPositiveRouteSourceProof:
    def test_demo_path_resolves_all_required_fields(self) -> None:
        proof = build_positive_proof(_REPO_ROOT)
        assert proof["errors"] == [], (
            "demo-path routing fields must resolve from the contract/authority:\n"
            + "\n".join(proof["errors"])
        )
        assert len(proof["entries"]) >= 1

    def test_entry_records_provider_model_endpoint_route_source(self) -> None:
        proof = build_positive_proof(_REPO_ROOT)
        entry = proof["entries"][0]
        for field in ("provider", "model", "endpoint_ref", "route_source"):
            assert entry[field], f"{field} must be resolved from authority, not blank"

    def test_each_field_has_source_file_line(self) -> None:
        proof = build_positive_proof(_REPO_ROOT)
        entry = proof["entries"][0]
        for key in _REQUIRED_ROUTING_KEYS:
            src = entry["field_sources"].get(key, "")
            assert ":" in src, f"field {key} must cite a source line, got {src!r}"
            assert "contract.yaml" in src, (
                f"field {key} must cite a contract source, got {src!r}"
            )

    def test_endpoint_resolves_from_bifrost_authority(self) -> None:
        proof = build_positive_proof(_REPO_ROOT)
        entry = proof["entries"][0]
        # endpoint may be null in the committed contract (overlay-merged at
        # runtime) — but the SOURCE of resolution must be the bifrost authority,
        # never an in-code literal or env var.
        assert "bifrost_delegation.yaml" in entry["endpoint_source"]
        assert entry["endpoint_ref"] in entry["endpoint_source"]


@pytest.mark.unit
class TestEndpointResolutionFailsClosed:
    def test_undeclared_endpoint_ref_returns_not_declared(self) -> None:
        # An endpoint_ref absent from the bifrost authority must surface a
        # NOT DECLARED source so the positive proof records it as an error
        # (fail-closed) rather than a silent PASS with no authority backing.
        _url, _key, source = _resolve_endpoint_for_ref(
            _REPO_ROOT, "this-backend-does-not-exist"
        )
        assert "NOT DECLARED" in source

    def test_undeclared_endpoint_ref_makes_positive_proof_fail(
        self, tmp_path: Path
    ) -> None:
        # Build a tmp repo whose demo contract points at an undeclared backend.
        import shutil

        src_contract = (
            _REPO_ROOT / "src/omnimarket/nodes/node_generation_consumer/contract.yaml"
        )
        dst_dir = tmp_path / "src/omnimarket/nodes/node_generation_consumer"
        dst_dir.mkdir(parents=True)
        text = src_contract.read_text(encoding="utf-8")
        text = text.replace("endpoint_ref: local-coder", "endpoint_ref: nonexistent")
        (dst_dir / "contract.yaml").write_text(text, encoding="utf-8")
        # copy the bifrost authority config (which lacks 'nonexistent')
        cfg_src = _REPO_ROOT / "src/omnimarket/configs/bifrost_delegation.yaml"
        cfg_dst = tmp_path / "src/omnimarket/configs/bifrost_delegation.yaml"
        cfg_dst.parent.mkdir(parents=True)
        shutil.copy(cfg_src, cfg_dst)
        (tmp_path / ".git").mkdir()

        proof = build_positive_proof(tmp_path)
        assert any("nonexistent" in e for e in proof["errors"]), proof["errors"]


@pytest.mark.unit
class TestNegativeAuditLiveDemoPath:
    def test_demo_path_is_clean(self) -> None:
        audit = build_negative_audit(_REPO_ROOT)
        assert audit["errors"] == [], "\n".join(audit["errors"])
        assert audit["clean"], (
            "demo-path negative audit found violations:\n"
            + "\n".join(v for fr in audit["files"] for v in fr["violations"])
        )

    def test_every_demo_source_scanned(self) -> None:
        audit = build_negative_audit(_REPO_ROOT)
        scanned = {fr["source"] for fr in audit["files"]}
        assert len(scanned) >= 3


@pytest.mark.unit
class TestApiKeyResolutionIsSanctioned:
    def test_api_key_ref_read_not_flagged(self) -> None:
        # os.environ[api_key_ref] is the canonical effect-boundary secret read.
        source = (
            "import os\n"
            "def f(api_key_ref):\n"
            "    return os.environ[api_key_ref].strip()\n"
        )
        violations = _scan_env_reads(source, source.splitlines())
        assert violations == []

    def test_api_key_env_get_not_flagged(self) -> None:
        source = 'import os\nvalue = os.environ.get("GEMINI_API_KEY", "")\n'
        violations = _scan_env_reads(source, source.splitlines())
        assert violations == []

    def test_is_api_key_resolution_classifier(self) -> None:
        assert _is_api_key_resolution("api_key_ref")
        assert _is_api_key_resolution("GEMINI_API_KEY")
        assert _is_api_key_resolution("OPENROUTER_API_KEY")
        assert not _is_api_key_resolution("LLM_CODER_URL")
        assert not _is_api_key_resolution("provider")


@pytest.mark.unit
class TestNegativeAuditCatchesViolations:
    def test_endpoint_env_read_is_flagged(self) -> None:
        source = 'import os\nendpoint = os.environ.get("LLM_CODER_URL", "")\n'
        violations = _scan_env_reads(source, source.splitlines())
        assert len(violations) == 1
        assert "LLM_CODER_URL" in violations[0][1]

    def test_provider_model_env_read_is_flagged(self) -> None:
        source = "import os\nmodel = os.getenv('LLM_MODEL')\n"
        violations = _scan_env_reads(source, source.splitlines())
        assert len(violations) == 1

    def test_skip_token_exempts_env_read(self) -> None:
        source = (
            'import os\nx = os.environ.get("LLM_CODER_URL")  # ONEX_FLAG_EXEMPT: test\n'
        )
        violations = _scan_env_reads(source, source.splitlines())
        assert violations == []

    def test_hardcoded_provider_literal_is_flagged(self) -> None:
        source = (
            "URL = 'https://generativelanguage.googleapis.com/v1/chat/completions'\n"
        )
        violations = _scan_literal_tokens(
            source,
            source.splitlines(),
            ("generativelanguage.googleapis.com",),
            "provider-literal",
        )
        assert len(violations) == 1

    def test_provider_literal_in_docstring_not_flagged(self) -> None:
        # A docstring documenting the provider is documentation, not behavior.
        source = (
            '"""This handler does NOT call generativelanguage.googleapis.com."""\n'
            "x = 1\n"
        )
        violations = _scan_literal_tokens(
            source,
            source.splitlines(),
            ("generativelanguage.googleapis.com",),
            "provider-literal",
        )
        assert violations == []

    def test_fallback_endpoint_literal_is_flagged(self) -> None:
        source = 'BASE = os.environ\nx = "OPENROUTER_BASE_URL value"\n'
        violations = _scan_literal_tokens(
            source,
            source.splitlines(),
            ("OPENROUTER_BASE_URL",),
            "fallback-endpoint-env",
        )
        assert len(violations) == 1


@pytest.mark.unit
class TestEvidencePacket:
    def test_packet_passes_for_live_demo_path(self) -> None:
        packet = build_evidence_packet(_REPO_ROOT)
        assert packet["passed"], packet
        assert packet["positive_ok"]
        assert packet["negative_ok"]

    def test_packet_records_demo_path_definition(self) -> None:
        packet = build_evidence_packet(_REPO_ROOT)
        assert packet["ticket"] == "OMN-12821"
        assert packet["gate"] == "routing-authority-demo-gate"
        assert len(packet["demo_path_contracts"]) >= 1
        assert len(packet["demo_path_sources"]) >= 3

    def test_packet_serializable(self) -> None:
        import json

        packet = build_evidence_packet(_REPO_ROOT)
        text = json.dumps(packet, sort_keys=True)
        assert "positive_proof" in text
        assert "negative_audit" in text

    def test_packet_includes_residue_and_shape_sections(self) -> None:
        """Extended packet (OMN-12877, OMN-12883) includes new audit sections."""
        packet = build_evidence_packet(_REPO_ROOT)
        assert "residue_audit" in packet
        assert "provider_endpoint_shape_audit" in packet
        assert "residue_ok" in packet
        assert "shape_ok" in packet
        assert "extension_tickets" in packet
        assert "OMN-12877" in packet["extension_tickets"]
        assert "OMN-12883" in packet["extension_tickets"]

    def test_packet_residue_and_shape_pass_on_live_repo(self) -> None:
        """Live repo passes the residue and shape audits."""
        packet = build_evidence_packet(_REPO_ROOT)
        assert packet["residue_ok"], "residue audit failed:\n" + "\n".join(
            packet["residue_audit"]["new_violations"]
        )
        assert packet["shape_ok"], "shape audit failed:\n" + "\n".join(
            packet["provider_endpoint_shape_audit"]["violations"]
        )


@pytest.mark.unit
class TestResidueAuditOMN12877:
    """Tests for the scope-extended residue audit (OMN-12877)."""

    def test_live_residue_files_within_baseline(self) -> None:
        """Live repo residue files must not exceed their baselined violation counts."""
        audit = build_residue_audit(_REPO_ROOT)
        assert audit["errors"] == [], "\n".join(audit["errors"])
        assert audit["clean"], (
            "residue audit found regression(s) above baseline:\n"
            + "\n".join(audit["new_violations"])
        )

    def test_residue_files_exist(self) -> None:
        """All configured residue source files must exist in the repo."""
        audit = build_residue_audit(_REPO_ROOT)
        assert audit["errors"] == [], "residue source files are missing:\n" + "\n".join(
            audit["errors"]
        )

    def test_residue_audit_records_all_configured_files(self) -> None:
        """Residue audit must produce a result entry for every configured file."""
        audit = build_residue_audit(_REPO_ROOT)
        scanned = {fr["source"] for fr in audit["files"]}
        for src_rel, _baseline, _ticket, _desc in _RESIDUE_SOURCES:
            assert src_rel in scanned, f"residue file {src_rel!r} was not scanned"

    def test_residue_audit_records_debt_tickets(self) -> None:
        """Each residue file entry must cite a debt ticket."""
        audit = build_residue_audit(_REPO_ROOT)
        for entry in audit["files"]:
            assert entry.get("debt_ticket"), (
                f"residue entry for {entry['source']!r} has no debt_ticket"
            )

    def test_cross_repo_debt_documented(self) -> None:
        """Cross-repo debt (omnibase_infra) must appear in the audit record."""
        audit = build_residue_audit(_REPO_ROOT)
        cross_repo = audit.get("cross_repo_debt", [])
        assert len(cross_repo) >= 1
        repos = {entry["repo"] for entry in cross_repo}
        assert "omnibase_infra" in repos

    def test_residue_regression_detected(self) -> None:
        """Gate must FAIL when violation count exceeds the baseline."""
        import check_routing_authority as mod

        original = mod._RESIDUE_SOURCES
        # Set baseline=1 for cli_ab_compare_suite — actual is 2, so regression
        mod._RESIDUE_SOURCES = (
            (
                "src/omnimarket/inference/bridge_config_loader.py",
                2,
                "OMN-12877",
                "test",
            ),
            (
                "src/omnimarket/cli/cli_ab_compare_suite.py",
                1,  # actual count is 2 → regression
                "OMN-12877",
                "test",
            ),
        )
        try:
            audit = build_residue_audit(_REPO_ROOT)
            assert not audit["clean"], (
                "gate must FAIL when violation count exceeds baseline"
            )
            assert len(audit["new_violations"]) >= 1
            assert "cli_ab_compare_suite.py" in audit["new_violations"][0]
        finally:
            mod._RESIDUE_SOURCES = original

    def test_contract_config_ok_annotation_exempts_env_read(self) -> None:
        """An env read annotated with 'contract-config-ok' is not a violation."""
        source = (
            "import os\n"
            "x = os.environ.get(\n"
            '    "OPENROUTER_BASE_URL", ""\n'
            ").strip()  # contract-config-ok: config\n"
        )
        violations = _scan_env_reads(source, source.splitlines())
        assert violations == [], (
            f"contract-config-ok annotation must exempt the env read, got: {violations}"
        )


@pytest.mark.unit
class TestProviderEndpointShapeOMN12883:
    """Tests for the provider-class endpoint URL shape audit (OMN-12883)."""

    def test_live_bifrost_config_is_shape_compliant(self) -> None:
        """The committed bifrost_delegation.yaml must pass the shape audit."""
        audit = build_provider_endpoint_shape_audit(_REPO_ROOT)
        assert audit["clean"], (
            "bifrost backends violate provider-class endpoint URL shape:\n"
            + "\n".join(audit["violations"])
        )

    def test_all_local_backends_have_null_endpoint_url(self) -> None:
        """Local backends must have endpoint_url=null (overlay-supplied)."""
        audit = build_provider_endpoint_shape_audit(_REPO_ROOT)
        for backend in audit["backends"]:
            if backend.get("tier") == "local":
                assert backend["endpoint_url"] is None, (
                    f"local backend {backend['backend_id']!r} must have "
                    f"endpoint_url=null, got {backend['endpoint_url']!r}"
                )

    def test_all_local_backends_declare_endpoint_url_env(self) -> None:
        """Local backends must declare endpoint_url_env (overlay source)."""
        audit = build_provider_endpoint_shape_audit(_REPO_ROOT)
        for backend in audit["backends"]:
            if backend.get("tier") == "local":
                assert backend["endpoint_url_env"], (
                    f"local backend {backend['backend_id']!r} must declare "
                    "endpoint_url_env for overlay-supplied endpoint"
                )

    def test_cloud_backend_missing_endpoint_url_is_violation(
        self, tmp_path: Path
    ) -> None:
        """A non-local backend with no endpoint_url_env and no endpoint_url must fail."""

        src_cfg = _REPO_ROOT / "src/omnimarket/configs/bifrost_delegation.yaml"
        data = yaml.safe_load(src_cfg.read_text(encoding="utf-8"))
        # Inject a rogue cloud backend with no endpoint_url and no endpoint_url_env
        data["backends"].append(
            {"backend_id": "rogue-cloud", "tier": "cheap_cloud", "endpoint_url": None}
        )
        cfg_dir = tmp_path / "src/omnimarket/configs"
        cfg_dir.mkdir(parents=True)
        (cfg_dir / "bifrost_delegation.yaml").write_text(
            yaml.dump(data), encoding="utf-8"
        )
        (tmp_path / ".git").mkdir()

        audit = build_provider_endpoint_shape_audit(tmp_path)
        assert not audit["clean"], (
            "rogue backend with no endpoint URL must fail shape audit"
        )
        assert any("rogue-cloud" in v for v in audit["violations"]), (
            f"rogue-cloud must appear in violations: {audit['violations']}"
        )

    def test_overlay_backend_with_nonnull_endpoint_url_is_violation(
        self, tmp_path: Path
    ) -> None:
        """A backend with endpoint_url_env set AND a non-null endpoint_url must fail."""
        data = {
            "backends": [
                {
                    "backend_id": "conflict-backend",
                    "tier": "local",
                    "endpoint_url_env": "BIFROST_LOCAL_CODER_ENDPOINT_URL",
                    "endpoint_url": "http://local-inference.internal:8000/v1/chat/completions",
                }
            ]
        }
        cfg_dir = tmp_path / "src/omnimarket/configs"
        cfg_dir.mkdir(parents=True)
        (cfg_dir / "bifrost_delegation.yaml").write_text(
            yaml.dump(data), encoding="utf-8"
        )
        (tmp_path / ".git").mkdir()

        audit = build_provider_endpoint_shape_audit(tmp_path)
        assert not audit["clean"], (
            "backend with both endpoint_url_env and non-null endpoint_url must fail"
        )
        assert any("conflict-backend" in v for v in audit["violations"]), (
            f"conflict-backend must appear in violations: {audit['violations']}"
        )

    def test_cli_agent_tier_exempt_from_endpoint_url_requirement(
        self, tmp_path: Path
    ) -> None:
        """CLI-agent tier backends may have empty endpoint_url (no HTTP call made)."""
        data = {
            "backends": [
                {
                    "backend_id": "cli-claude",
                    "tier": "cli_agents",
                    "endpoint_url": "",
                }
            ]
        }
        cfg_dir = tmp_path / "src/omnimarket/configs"
        cfg_dir.mkdir(parents=True)
        (cfg_dir / "bifrost_delegation.yaml").write_text(
            yaml.dump(data), encoding="utf-8"
        )
        (tmp_path / ".git").mkdir()

        audit = build_provider_endpoint_shape_audit(tmp_path)
        assert audit["clean"], (
            f"cli_agents tier must be exempt from endpoint_url requirement: "
            f"{audit['violations']}"
        )
