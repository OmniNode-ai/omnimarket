# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Tests for the routing-authority demo gate (OMN-12821, plan A1.5).

The gate is a DEMO GATE proving two things about the exact demo path:

POSITIVE: provider, model (served_model_id), endpoint_ref, resolved endpoint,
    and route_source all resolve from contract / overlay / routing authority,
    with the source file/line recorded for each.

NEGATIVE: no demo-path source reads env vars for endpoint/provider/model, no
    hardcoded provider literals, and no fallback endpoint strings after route
    resolution. Secret-value resolution (os.environ[api_key_ref]) at the effect
    boundary is the SANCTIONED contract-native pattern and is NOT flagged.

These tests assert the live demo path passes AND that the gate actually fails
when a violation is injected (proving it is enforcing, not a rubber stamp).

Ticket: OMN-12821
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SCRIPTS_CI = Path(__file__).parent.parent / "scripts" / "ci"
sys.path.insert(0, str(_SCRIPTS_CI))

from check_routing_authority import (  # noqa: E402
    _REQUIRED_ROUTING_KEYS,
    _is_api_key_resolution,
    _scan_env_reads,
    _scan_literal_tokens,
    build_evidence_packet,
    build_negative_audit,
    build_positive_proof,
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
