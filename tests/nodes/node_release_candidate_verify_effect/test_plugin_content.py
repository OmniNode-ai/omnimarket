# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The plugin content check: identity by version and file content across the guide merge (OMN-23343)."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_release_candidate_verify_effect.handlers.handler_release_candidate_verify import (
    HandlerReleaseCandidateVerify,
)
from omnimarket.nodes.node_release_candidate_verify_effect.models.enum_release_candidate_finding import (
    EnumReleaseCandidateFinding as K,
)
from omnimarket.nodes.node_release_candidate_verify_effect.models.model_release_candidate_manifest import (
    ModelReleaseCandidateManifest,
)
from omnimarket.nodes.node_release_candidate_verify_effect.models.model_release_candidate_verify_request import (
    ModelInstalledPluginRecord,
    ModelReleaseCandidateVerifyRequest,
)
from omnimarket.nodes.node_release_candidate_verify_effect.models.model_release_candidate_verify_result import (
    ModelReleaseCandidateVerifyResult,
)
from tests.nodes.node_release_candidate_verify_effect.support import write_plugin

pytestmark = pytest.mark.unit

AFTER_GUIDE_MERGE_COMMIT = "fedcba9876543210"


def _install(tmp_path: Path, version: str = "0.3.3") -> ModelInstalledPluginRecord:
    """A plugin install recorded at an omniclaude commit after the manifest's, with Claude Code's .in_use."""
    write_plugin(tmp_path)
    (tmp_path / ".in_use").mkdir()
    (tmp_path / ".in_use" / "session").write_text("1")
    return ModelInstalledPluginRecord(
        version=version,
        install_path=str(tmp_path),
        git_commit_sha=AFTER_GUIDE_MERGE_COMMIT,
    )


def _verify(
    manifest: ModelReleaseCandidateManifest, record: ModelInstalledPluginRecord
) -> ModelReleaseCandidateVerifyResult:
    return HandlerReleaseCandidateVerify().handle(
        ModelReleaseCandidateVerifyRequest(
            operation="verify_plugin", manifest=manifest, plugin_record=record
        )
    )


def test_plugin_identity_survives_guide_merge(
    manifest: ModelReleaseCandidateManifest, tmp_path: Path
) -> None:
    result = _verify(manifest, _install(tmp_path))
    assert result.plugin_matches_manifest is True, result.findings
    assert result.reading == "candidate"
    assert (
        result.installed_git_commit_sha
        == AFTER_GUIDE_MERGE_COMMIT
        != manifest.plugin.omniclaude_commit
    )


def test_plugin_file_changed_not_candidate(
    manifest: ModelReleaseCandidateManifest, tmp_path: Path
) -> None:
    record = _install(tmp_path)
    (tmp_path / "commands/delegate.md").write_text("delegate!\n")
    result = _verify(manifest, record)
    assert (result.plugin_matches_manifest, result.reading) == (
        False,
        "not-the-candidate",
    )
    assert [(f.kind, f.subject) for f in result.findings] == [
        (K.PLUGIN_FILE_MODIFIED, "commands/delegate.md")
    ]


def test_plugin_extra_file_not_candidate(
    manifest: ModelReleaseCandidateManifest, tmp_path: Path
) -> None:
    record = _install(tmp_path)
    (tmp_path / "commands/extra.md").write_text("x")
    result = _verify(manifest, record)
    assert (result.plugin_matches_manifest, result.reading) == (
        False,
        "not-the-candidate",
    )
    assert [(f.kind, f.subject) for f in result.findings] == [
        (K.PLUGIN_EXTRA_FILE, "commands/extra.md")
    ]


def test_plugin_version_differs_not_candidate(
    manifest: ModelReleaseCandidateManifest, tmp_path: Path
) -> None:
    result = _verify(manifest, _install(tmp_path, version="0.3.4"))
    assert (result.plugin_matches_manifest, result.reading) == (
        False,
        "not-the-candidate",
    )
    assert [f.kind for f in result.findings] == [K.PLUGIN_VERSION]


def test_plugin_file_missing_not_candidate(
    manifest: ModelReleaseCandidateManifest, tmp_path: Path
) -> None:
    record = _install(tmp_path)
    (tmp_path / "hooks/hooks.json").unlink()
    result = _verify(manifest, record)
    assert result.plugin_matches_manifest is False
    assert [f.kind for f in result.findings] == [K.PLUGIN_FILE_MISSING]
