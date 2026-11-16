# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One set of stub candidate wheels and one manifest for the session."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_release_candidate_verify_effect.models.model_release_candidate_manifest import (
    ModelReleaseCandidateManifest,
)
from tests.nodes.node_release_candidate_verify_effect.support import (
    build_candidate_wheels,
    candidate_manifest,
    write_plugin,
)


@pytest.fixture(scope="session")
def wheel_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("candidate-wheels")
    build_candidate_wheels(path)
    return path


@pytest.fixture(scope="session")
def manifest(
    wheel_dir: Path, tmp_path_factory: pytest.TempPathFactory
) -> ModelReleaseCandidateManifest:
    wheels = {p.name.split("-")[0]: p for p in wheel_dir.glob("*.whl")}
    plugin_files = write_plugin(tmp_path_factory.mktemp("plugin-source"))
    return candidate_manifest(wheels, plugin_files)
