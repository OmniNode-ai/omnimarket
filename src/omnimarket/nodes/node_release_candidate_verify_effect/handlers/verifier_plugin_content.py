# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The plugin content check: identity by version and file content, never by commit."""

from __future__ import annotations

from pathlib import Path

from omnimarket.nodes.node_release_candidate_verify_effect.handlers.verifier_installed_content import (
    sha256_file,
)
from omnimarket.nodes.node_release_candidate_verify_effect.models.enum_release_candidate_finding import (
    EnumReleaseCandidateFinding as K,
)
from omnimarket.nodes.node_release_candidate_verify_effect.models.model_release_candidate_manifest import (
    ModelManifestPlugin,
)
from omnimarket.nodes.node_release_candidate_verify_effect.models.model_release_candidate_overlay import (
    ModelReleaseCandidateOverlay,
)
from omnimarket.nodes.node_release_candidate_verify_effect.models.model_release_candidate_verify_request import (
    ModelInstalledPluginRecord,
)
from omnimarket.nodes.node_release_candidate_verify_effect.models.model_release_candidate_verify_result import (
    ModelReleaseCandidateFinding as Finding,
)


def verify_plugin(
    plugin: ModelManifestPlugin,
    overlay: ModelReleaseCandidateOverlay,
    record: ModelInstalledPluginRecord,
) -> list[Finding]:
    findings: list[Finding] = []
    if record.version != plugin.version:
        findings.append(
            Finding(
                kind=K.PLUGIN_VERSION,
                subject="version",
                detail=f"installed {record.version}, manifest {plugin.version}",
            )
        )
    root = Path(record.install_path)
    listed = {entry.path for entry in plugin.files}
    for entry in plugin.files:
        target = root / entry.path
        if not target.is_file():
            findings.append(Finding(kind=K.PLUGIN_FILE_MISSING, subject=entry.path))
        elif sha256_file(target) != entry.sha256:
            findings.append(Finding(kind=K.PLUGIN_FILE_MODIFIED, subject=entry.path))
    if root.is_dir():
        for path in sorted(root.rglob("*")):
            relative = path.relative_to(root)
            if not path.is_file() or relative.parts[0] in overlay.plugin_ignored_names:
                continue
            if relative.as_posix() not in listed:
                findings.append(
                    Finding(kind=K.PLUGIN_EXTRA_FILE, subject=relative.as_posix())
                )
    return findings
