# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Cross-repo contract dependency validation (OMN-10908).

The ``node_delegate_skill_orchestrator`` contract declares
``cross_repo_dependencies`` against ``omnibase_infra/node_delegation_orchestrator``.
If omnibase_infra renames a delegation topic, this gate must fail so the drift is
caught before it reaches the runtime.

Resolution strategy:
- the omnimarket contract is resolved from the installed ``omnimarket`` package
  (cwd-independent);
- each cross-repo contract is resolved from ``$OMNI_HOME/<repo>/src/<repo>/nodes/<node>/contract.yaml``.

If ``OMNI_HOME`` is unset or not a directory the test skips with a clear message
(local unit runs). In CI ``OMNI_HOME`` is set to the canonical clone root, so a
missing referenced contract is a hard failure.
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
from pathlib import Path

import pytest
import yaml

_OMNI_HOME_RAW = os.environ.get("OMNI_HOME", "")
_OMNI_HOME = Path(_OMNI_HOME_RAW) if _OMNI_HOME_RAW else None


def _omnimarket_package_root() -> Path:
    spec = importlib.util.find_spec("omnimarket")
    assert spec is not None, "omnimarket package spec not found"
    assert spec.origin is not None, "omnimarket package origin not found"
    return Path(spec.origin).parent


def _delegate_skill_contract() -> dict[str, object]:
    contract_path = (
        _omnimarket_package_root()
        / "nodes"
        / "node_delegate_skill_orchestrator"
        / "contract.yaml"
    )
    assert contract_path.exists(), (
        f"delegate skill orchestrator contract not found at {contract_path}"
    )
    loaded: dict[str, object] = yaml.safe_load(contract_path.read_text())
    return loaded


def _cross_repo_contract_path(repo: str, node: str) -> Path:
    assert _OMNI_HOME is not None
    return _OMNI_HOME / repo / "src" / repo / "nodes" / node / "contract.yaml"


@pytest.mark.integration
@pytest.mark.skipif(
    _OMNI_HOME is None or not _OMNI_HOME.is_dir(),
    reason="OMNI_HOME is not set or not a directory — local unit run; required in CI",
)
def test_cross_repo_dependencies_declared() -> None:
    contract = _delegate_skill_contract()
    deps = contract.get("cross_repo_dependencies", [])
    assert isinstance(deps, list), "cross_repo_dependencies must be a list"
    assert deps, "delegate skill contract must declare cross_repo_dependencies"


@pytest.mark.integration
@pytest.mark.skipif(
    _OMNI_HOME is None or not _OMNI_HOME.is_dir(),
    reason="OMNI_HOME is not set or not a directory — local unit run; required in CI",
)
def test_cross_repo_dependency_topics_resolve_in_referenced_contracts() -> None:
    contract = _delegate_skill_contract()
    deps = contract.get("cross_repo_dependencies", [])
    assert isinstance(deps, list)

    for dep in deps:
        assert isinstance(dep, dict)
        repo = dep["repo"]
        node = dep["node"]
        ref_path = _cross_repo_contract_path(repo, node)
        assert ref_path.exists(), (
            f"cross-repo dependency {repo}/{node} contract not found at {ref_path}"
        )

        ref_contract = yaml.safe_load(ref_path.read_text())
        contract_hash = hashlib.sha256(ref_path.read_bytes()).hexdigest()[:12]
        ref_event_bus = ref_contract["event_bus"]
        ref_subscribe = set(ref_event_bus["subscribe_topics"])
        ref_publish = set(ref_event_bus["publish_topics"])
        ref_all = ref_subscribe | ref_publish

        for topic in dep.get("required_topics", []):
            assert topic in ref_all, (
                f"cross-repo required topic {topic!r} not declared in "
                f"{repo}/{node} contract event_bus topics "
                f"(path={ref_path}, sha256[:12]={contract_hash})"
            )
        for topic in dep.get("terminal_events", []):
            assert topic in ref_publish, (
                f"cross-repo terminal event {topic!r} not declared in "
                f"{repo}/{node} contract publish_topics "
                f"(path={ref_path}, sha256[:12]={contract_hash})"
            )
