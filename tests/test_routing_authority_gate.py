# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Integration tests for the routing-authority demo gate (OMN-13285, W6/GAP-3).

The gate logic was ported out of the deleted omnimarket-local
``scripts/ci/check_routing_authority.py`` into the canonical core COMPUTE
validator ``omnibase_core.validation.validator_routing_authority`` (which folds
in the previously-unwired ``validator_delegation_profile``). The validator's own
positive/negative unit suite lives in omnibase_core
(``tests/unit/validation/test_validator_routing_authority.py``).

These omnimarket-side tests assert the integration contract: the core validator,
run against the LIVE omnimarket tree via its CLI/EFFECT boundary, PASSES on the
committed demo path AND FAILS when a violation is injected into the corpus — i.e.
the wired gate is enforcing, not a rubber stamp.

Tickets: OMN-13285 (port), originating OMN-12821 / OMN-12877 / OMN-12883.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from omnibase_core.validation.validator_routing_authority import (
    build_input_from_disk,
    evaluate,
    main,
)

_REPO_ROOT = Path(__file__).parent.parent


@pytest.mark.unit
class TestLiveDemoPathPasses:
    def test_live_omnimarket_tree_passes(self) -> None:
        payload, missing, present = build_input_from_disk("omnimarket", _REPO_ROOT)
        assert present > 0, "omnimarket must host the routing-authority demo path"
        assert missing == [], f"configured demo-path artifacts missing: {missing}"
        report = evaluate(payload)
        assert report.passed, [f"{f.location}: {f.message}" for f in report.findings]

    def test_cli_exit_zero_on_live_tree(self) -> None:
        rc = main(["--repo", "omnimarket", "--repo-root", str(_REPO_ROOT)])
        assert rc == 0

    def test_demo_path_artifacts_present(self) -> None:
        _payload, missing, present = build_input_from_disk("omnimarket", _REPO_ROOT)
        # 1 contract + 3 sources + 3 residue + 1 bifrost config = 8 expected.
        assert present == 8, f"expected 8 demo-path artifacts, found {present}"
        assert missing == []


@pytest.mark.unit
class TestGateIsEnforcing:
    def test_injected_env_read_fails(self, tmp_path: Path) -> None:
        # Build a minimal tree: a demo-path source that reads a model env var.
        src_dir = (
            tmp_path / "src/omnimarket/nodes/node_llm_delegation_call_effect/handlers"
        )
        src_dir.mkdir(parents=True)
        (src_dir / "handler_inference_intent.py").write_text(
            "import os\nmodel = os.environ['DELEGATION_PROVIDER']\n",
            encoding="utf-8",
        )
        # Provide a valid contract + bifrost so only the env read fails.
        contract_dir = tmp_path / "src/omnimarket/nodes/node_generation_consumer"
        contract_dir.mkdir(parents=True)
        (contract_dir / "contract.yaml").write_text(
            "model_routing:\n"
            "  provider: local\n"
            "  served_model_id: qwen\n"
            "  endpoint_ref: local-coder\n"
            "  routing_source: contract\n",
            encoding="utf-8",
        )
        cfg_dir = tmp_path / "src/omnimarket/configs"
        cfg_dir.mkdir(parents=True)
        (cfg_dir / "bifrost_delegation.yaml").write_text(
            "backends:\n  - backend_id: local-coder\n    endpoint_url_env: X\n    endpoint_url: null\n",
            encoding="utf-8",
        )
        payload, _missing, _present = build_input_from_disk("omnimarket", tmp_path)
        report = evaluate(payload)
        assert not report.passed
        assert any(f.rule_id == "negative-audit" for f in report.findings)

    def test_injected_cli_backend_fails(self, tmp_path: Path) -> None:
        cfg_dir = tmp_path / "src/omnimarket/configs"
        cfg_dir.mkdir(parents=True)
        (cfg_dir / "bifrost_delegation.yaml").write_text(
            "backends:\n  - backend_id: cli-codex\n    tier: cli_agents\n    endpoint_url: cli://codex\n",
            encoding="utf-8",
        )
        payload, _missing, present = build_input_from_disk("omnimarket", tmp_path)
        assert present > 0
        report = evaluate(payload)
        assert not report.passed
        assert any(
            "shelled-CLI backends are forbidden" in f.message for f in report.findings
        )
