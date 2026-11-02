# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Temporary registry and overlay fixtures keep grammar checks isolated."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.handlers import ledger_claim_contract as claim_contract

pytestmark = pytest.mark.unit

REGISTRY_ENV = "OMNI_HOME"


@pytest.fixture(autouse=True)
def isolated_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(REGISTRY_ENV, str(tmp_path / "registry"))
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path / "empty-overlays"))
    monkeypatch.delenv("ONEX_WORKTREES_ROOT", raising=False)
    monkeypatch.delenv(claim_contract.REQUIRE_CLAIM_CONTRACT_ENV, raising=False)


@pytest.fixture
def repos_env(tmp_path: Path) -> dict[str, str]:
    root = tmp_path / "overlays"
    path = root / "repos" / "overlay.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(
        yaml.safe_dump(
            {
                "plugin_source": "repo-plugin-source",
                "private_data": "repo-private-data",
                "knowledge_base": "repo-knowledge-base",
                "deploy": "repo-deploy",
                "private_data_home_envs": [
                    "EXAMPLE_PRIVATE_DATA_HOME",
                    "EXAMPLE_PRIVATE_DATA_PATH",
                ],
                "knowledge_base_home_env": "EXAMPLE_KNOWLEDGE_BASE_PATH",
                "beside_root": ["private_data", "knowledge_base"],
            }
        ),
        encoding="utf-8",
    )
    return {
        "ONEX_SKILL_OVERLAY_ROOTS": str(root),
        REGISTRY_ENV: str(tmp_path / "registry"),
        "EXAMPLE_PRIVATE_DATA_HOME": str(tmp_path / "private-clone"),
    }


@pytest.fixture
def registry(repos_env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> Path:
    for key, value in repos_env.items():
        monkeypatch.setenv(key, value)
    root = Path(repos_env[REGISTRY_ENV])
    root.mkdir()
    return root
