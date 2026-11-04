# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Every test in this directory reads the synthetic overlay tree unless it sets its own.

The overlay uses neutral names and documentation-range addresses only; no test reads a real overlay.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

OVERLAY_DOCUMENTS: dict[str, dict[str, Any]] = {
    "lab_hosts": {
        "hosts": [{"name": "n201", "target": "192.0.2.41"}],
        "registry_root_candidates": ["/srv/example/registry", "$HOME/registry"],
    },
    "repos": {
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
    },
    "hygiene_vocab": {
        "vocabulary_rel": "docs/example_vocabulary.yaml",
        "lab_vocabulary_rel": "vocabularies/example_lab_vocabulary.yaml",
    },
}


def write_overlays(root: Path, documents: dict[str, dict[str, Any]]) -> Path:
    for overlay_id, document in documents.items():
        path = root / overlay_id / "overlay.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return root


@pytest.fixture(autouse=True)
def _overlay_roots(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> Path:
    root = write_overlays(tmp_path_factory.mktemp("overlays"), OVERLAY_DOCUMENTS)
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(root))
    for name in ("ONEX_LAB_RUN_HOSTS", "LANDING_POOL_HOSTS", "ONEX_REGISTRY_ROOT"):
        monkeypatch.delenv(name, raising=False)
    return root


@pytest.fixture
def overlay_writer(
    tmp_path_factory: pytest.TempPathFactory,
) -> Callable[[dict[str, dict[str, Any]]], Path]:
    def write(documents: dict[str, dict[str, Any]]) -> Path:
        return write_overlays(tmp_path_factory.mktemp("other-overlays"), documents)

    return write
