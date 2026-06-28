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
from omnibase_core.nodes.node_routing_authority_check_compute.handler import (
    ModelResidueEntry,
    check_routing_authority_at_path,
)
from omnibase_core.validation.validator_routing_authority import (
    main,
)

# OMN-13695: baseline bumped from 6 → 8 to accommodate openai + google policy entries
# added to model_policy.yaml as part of migrating raw os.environ.get reads to
# ModelPolicyLoader routing authority.
_YAML_RESIDUE_BASELINE = 8
_YAML_POLICY_RESIDUE: tuple[ModelResidueEntry, ...] = (
    ModelResidueEntry(
        file_rel="src/omnimarket/model_policy.yaml",
        baseline_count=_YAML_RESIDUE_BASELINE,
        debt_ticket="OMN-12877",
        description=(
            f"model_policy.yaml carries {_YAML_RESIDUE_BASELINE} env_var declarations "
            "pending bifrost authority migration"
        ),
    ),
)

_REPO_ROOT = Path(__file__).parent.parent
_DEFAULT_CONTRACTS = ("src/omnimarket/nodes/node_generation_consumer/contract.yaml",)
_DEFAULT_SOURCES = (
    "src/omnimarket/nodes/node_generation_consumer/handlers/handler_generation_consumer.py",
    "src/omnimarket/nodes/node_llm_delegation_call_effect/handlers/handler_inference_intent.py",
    "src/omnimarket/adapters/llm/bifrost/config_loader_bifrost_delegation.py",
)
_DEFAULT_BIFROST = "src/omnimarket/configs/bifrost_delegation.yaml"


def _run_gate(repo_root: Path = _REPO_ROOT):
    return check_routing_authority_at_path(
        repo_root=repo_root,
        demo_path_contracts=_DEFAULT_CONTRACTS,
        demo_path_sources=_DEFAULT_SOURCES,
        bifrost_config_rel=_DEFAULT_BIFROST,
    )


def _violations(report: dict[str, object], key: str) -> list[str]:
    entries = report.get(key, [])
    if not isinstance(entries, list):
        return []
    found: list[str] = []
    for entry in entries:
        if isinstance(entry, dict):
            values = entry.get("violations", [])
            if isinstance(values, list):
                found.extend(str(value) for value in values)
    return found


@pytest.mark.unit
class TestLiveDemoPathPasses:
    def test_live_omnimarket_tree_passes(self) -> None:
        report = _run_gate()
        assert report.passed, report.model_dump()

    def test_yaml_residue_passes_with_current_baseline(self) -> None:
        """Assert model_policy.yaml env_var count does not exceed the tracked baseline.

        Baseline is managed here (not in omnibase_core) to allow per-PR bumps when
        canonical policies are added (OMN-13695). Decrease-only ratchet applies:
        do NOT increase _YAML_RESIDUE_BASELINE without a corresponding new policy entry.
        """
        report = check_routing_authority_at_path(
            repo_root=_REPO_ROOT,
            demo_path_contracts=_DEFAULT_CONTRACTS,
            demo_path_sources=_DEFAULT_SOURCES,
            bifrost_config_rel=_DEFAULT_BIFROST,
            yaml_policy_residue=_YAML_POLICY_RESIDUE,
        )
        assert report.passed, report.model_dump()

    def test_cli_exit_zero_on_live_tree(self) -> None:
        # Pass --no-default-residue so the CLI doesn't use the omnibase_core-internal
        # baseline (which may be stale when new canonical policies are added).
        # The yaml residue check is covered by test_yaml_residue_passes_with_current_baseline.
        rc = main(["--repo-root", str(_REPO_ROOT), "--no-default-residue"])
        assert rc == 0

    def test_demo_path_artifacts_present(self) -> None:
        required = (*_DEFAULT_CONTRACTS, *_DEFAULT_SOURCES, _DEFAULT_BIFROST)
        missing = [rel for rel in required if not (_REPO_ROOT / rel).exists()]
        assert missing == [], f"configured demo-path artifacts missing: {missing}"


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
        report = check_routing_authority_at_path(
            repo_root=tmp_path,
            demo_path_contracts=(
                "src/omnimarket/nodes/node_generation_consumer/contract.yaml",
            ),
            demo_path_sources=(
                "src/omnimarket/nodes/node_llm_delegation_call_effect/handlers/handler_inference_intent.py",
            ),
            bifrost_config_rel="src/omnimarket/configs/bifrost_delegation.yaml",
        )
        assert not report.passed
        assert any(
            "env-read" in violation
            for violation in _violations(report.negative_audit, "files")
        )

    def test_injected_cli_backend_fails(self, tmp_path: Path) -> None:
        src_dir = tmp_path / "src/omnimarket/nodes/node_generation_consumer/handlers"
        src_dir.mkdir(parents=True)
        (src_dir / "handler_generation_consumer.py").write_text(
            "def handle(payload):\n    return payload\n",
            encoding="utf-8",
        )
        contract_dir = tmp_path / "src/omnimarket/nodes/node_generation_consumer"
        contract_dir.mkdir(parents=True, exist_ok=True)
        (contract_dir / "contract.yaml").write_text(
            "model_routing:\n"
            "  provider: local\n"
            "  served_model_id: qwen\n"
            "  endpoint_ref: cli-codex\n"
            "  routing_source: contract\n",
            encoding="utf-8",
        )
        cfg_dir = tmp_path / "src/omnimarket/configs"
        cfg_dir.mkdir(parents=True)
        (cfg_dir / "bifrost_delegation.yaml").write_text(
            "backends:\n  - backend_id: cli-codex\n    tier: cli_agents\n    endpoint_url: cli://codex\n",
            encoding="utf-8",
        )
        report = check_routing_authority_at_path(
            repo_root=tmp_path,
            demo_path_contracts=(
                "src/omnimarket/nodes/node_generation_consumer/contract.yaml",
            ),
            demo_path_sources=(
                "src/omnimarket/nodes/node_generation_consumer/handlers/handler_generation_consumer.py",
            ),
            bifrost_config_rel="src/omnimarket/configs/bifrost_delegation.yaml",
        )
        assert not report.passed
        assert any(
            "shelled-CLI backends are forbidden" in violation
            for violation in report.provider_endpoint_shape_audit["violations"]
        )
