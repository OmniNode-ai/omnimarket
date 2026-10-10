# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The orchestrator's operator config, read from its contract (OMN-20866).

The ``landing_config`` block of node_pr_landing_orchestrator's contract.yaml
declares the GitHub mode (a default and one per repository), the operations
that stay dry_run everywhere, the repositories that need no change-control
companion, land through a merge queue, need no conversation resolution or are
prompted by the PR watcher's observations, and the arm gate's policy. The
handler builds :class:`PrLandingOrchestratorConfig` from it, so the contract,
not a code default, decides whether a mutation is sent.

The ``landing_gate_facts`` block beside it declares the ledger facts every
green head is checked against before an arm (the lab-proof repositories, the
ledger projection's freshness bound and the projections read); it is required,
so the runtime never arms without reading them.

A malformed block is refused at load: an unknown key, mode or operation is an
error, never a silent fallback to the shadow defaults.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import yaml

from omnimarket.events.pr_arm_gate import ModelArmGatePolicy
from omnimarket.events.pr_landing_github.enum_pr_landing_github_mode import (
    EnumPrLandingGithubMode,
)
from omnimarket.events.pr_landing_github.enum_pr_landing_github_operation import (
    EnumPrLandingGithubOperation,
)
from omnimarket.nodes.node_pr_landing_orchestrator.orchestration.core import (
    PrLandingGateFactsConfig,
    PrLandingOrchestratorConfig,
)

CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"
CONFIG_KEY = "landing_config"
GATE_FACTS_KEY = "landing_gate_facts"

_KEYS = frozenset(
    {
        "github_mode",
        "github_mode_by_repository",
        "dry_run_operations",
        "companion_exempt_repositories",
        "queue_repositories",
        "review_threads_not_required_repositories",
        "observed_prompt_repositories",
        "arm_policy",
    }
)
_READS = frozenset(
    {
        EnumPrLandingGithubOperation.READ_HEAD_CHECKS,
        EnumPrLandingGithubOperation.READ_PR_STATE,
    }
)


_GATE_KEYS = frozenset(
    {"lab_proof_repositories", "ledger_freshness_bound_minutes", "source"}
)
_SOURCE_KEYS = frozenset(
    {
        "dsn_env",
        "hold_state_relation",
        "ledger_rows_relation",
        "lab_proof_receipts_relation",
    }
)
_RELATION_RE = re.compile(r"^[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*$")
_ENV_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


class PrLandingContractConfigError(ValueError):
    """The contract's landing_config block is missing or malformed."""


def _repositories(block: Mapping[str, object], key: str) -> frozenset[str]:
    value = block.get(key, [])
    if not isinstance(value, list) or not all(
        isinstance(v, str) and "/" in v for v in value
    ):
        msg = f"{CONFIG_KEY}.{key} must be a list of owner/name repositories"
        raise PrLandingContractConfigError(msg)
    return frozenset(value)


def _mode(value: object, where: str) -> EnumPrLandingGithubMode:
    try:
        return EnumPrLandingGithubMode(str(value))
    except ValueError:
        msg = f"{where} is {value!r}, not one of dry_run or enforce"
        raise PrLandingContractConfigError(msg) from None


def config_from_block(block: object) -> PrLandingOrchestratorConfig:
    """The config one ``landing_config`` mapping declares."""
    if not isinstance(block, Mapping):
        msg = f"the contract declares no {CONFIG_KEY} mapping"
        raise PrLandingContractConfigError(msg)
    unknown = set(block) - _KEYS
    if unknown:
        msg = f"{CONFIG_KEY} has unknown keys {sorted(unknown)}"
        raise PrLandingContractConfigError(msg)
    if "github_mode" not in block:
        msg = f"{CONFIG_KEY}.github_mode is required"
        raise PrLandingContractConfigError(msg)
    by_repository = block.get("github_mode_by_repository", {})
    if not isinstance(by_repository, Mapping):
        msg = f"{CONFIG_KEY}.github_mode_by_repository must be a mapping"
        raise PrLandingContractConfigError(msg)
    operations = block.get("dry_run_operations", [])
    if not isinstance(operations, list):
        msg = f"{CONFIG_KEY}.dry_run_operations must be a list"
        raise PrLandingContractConfigError(msg)
    try:
        dry_run_operations = frozenset(
            EnumPrLandingGithubOperation(str(op)) for op in operations
        )
    except ValueError as exc:
        msg = f"{CONFIG_KEY}.dry_run_operations: {exc}"
        raise PrLandingContractConfigError(msg) from None
    if dry_run_operations & _READS:
        msg = f"{CONFIG_KEY}.dry_run_operations names a read; reads are always sent"
        raise PrLandingContractConfigError(msg)
    policy = block.get("arm_policy", {})
    try:
        arm_policy = ModelArmGatePolicy.model_validate(policy)
    except ValueError as exc:
        msg = f"{CONFIG_KEY}.arm_policy: {exc}"
        raise PrLandingContractConfigError(msg) from None
    return PrLandingOrchestratorConfig(
        github_mode=_mode(block["github_mode"], f"{CONFIG_KEY}.github_mode"),
        github_mode_by_repository={
            str(repo): _mode(mode, f"{CONFIG_KEY}.github_mode_by_repository.{repo}")
            for repo, mode in by_repository.items()
        },
        dry_run_operations=dry_run_operations,
        arm_policy=arm_policy,
        companion_exempt_repos=_repositories(block, "companion_exempt_repositories"),
        queue_repos=_repositories(block, "queue_repositories"),
        review_threads_not_required_repos=_repositories(
            block, "review_threads_not_required_repositories"
        ),
        observed_prompt_repos=_repositories(block, "observed_prompt_repositories"),
    )


def gate_facts_from_block(block: object) -> PrLandingGateFactsConfig:
    """The gate facts one ``landing_gate_facts`` mapping declares (OMN-20866)."""
    where = GATE_FACTS_KEY
    if not isinstance(block, Mapping):
        msg = f"the contract declares no {where} mapping"
        raise PrLandingContractConfigError(msg)
    unknown = set(block) - _GATE_KEYS
    if unknown:
        msg = f"{where} has unknown keys {sorted(unknown)}"
        raise PrLandingContractConfigError(msg)
    repositories = block.get("lab_proof_repositories")
    if not isinstance(repositories, list) or not all(
        isinstance(v, str) and "/" in v for v in repositories
    ):
        msg = (
            f"{where}.lab_proof_repositories must be a list of owner/name repositories"
        )
        raise PrLandingContractConfigError(msg)
    minutes = block.get("ledger_freshness_bound_minutes")
    if isinstance(minutes, bool) or not isinstance(minutes, int) or minutes < 1:
        msg = f"{where}.ledger_freshness_bound_minutes must be a positive integer"
        raise PrLandingContractConfigError(msg)
    source = block.get("source")
    if not isinstance(source, Mapping) or set(source) != _SOURCE_KEYS:
        msg = f"{where}.source must name exactly {sorted(_SOURCE_KEYS)}"
        raise PrLandingContractConfigError(msg)
    if not _ENV_RE.match(str(source["dsn_env"])):
        msg = f"{where}.source.dsn_env must be an environment variable name"
        raise PrLandingContractConfigError(msg)
    relations = {k: str(v) for k, v in source.items() if k != "dsn_env"}
    for key, relation in relations.items():
        if not _RELATION_RE.match(relation):
            msg = f"{where}.source.{key} must be a schema.table relation"
            raise PrLandingContractConfigError(msg)
    return PrLandingGateFactsConfig(
        lab_proof_repos=frozenset(repositories),
        ledger_freshness_bound=timedelta(minutes=minutes),
        dsn_env=str(source["dsn_env"]),
        hold_state_relation=relations["hold_state_relation"],
        ledger_rows_relation=relations["ledger_rows_relation"],
        lab_proof_receipts_relation=relations["lab_proof_receipts_relation"],
    )


def load_contract_config(path: Path = CONTRACT_PATH) -> PrLandingOrchestratorConfig:
    """The config node_pr_landing_orchestrator's contract.yaml declares."""
    with path.open(encoding="utf-8") as stream:
        contract = yaml.safe_load(stream)
    if not isinstance(contract, Mapping):
        msg = f"{path} is not a contract mapping"
        raise PrLandingContractConfigError(msg)
    return replace(
        config_from_block(contract.get(CONFIG_KEY)),
        gate_facts=gate_facts_from_block(contract.get(GATE_FACTS_KEY)),
    )


__all__: list[str] = [
    "CONFIG_KEY",
    "CONTRACT_PATH",
    "GATE_FACTS_KEY",
    "PrLandingContractConfigError",
    "config_from_block",
    "gate_facts_from_block",
    "load_contract_config",
]
