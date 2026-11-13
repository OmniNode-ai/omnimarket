# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Verify an installed environment, a plugin install, PyPI or the cut against the candidate manifest."""

from __future__ import annotations

from importlib.resources import files

import yaml

from omnimarket.nodes.node_release_candidate_verify_effect.handlers.verifier_installed_content import (
    verify_installed,
)
from omnimarket.nodes.node_release_candidate_verify_effect.handlers.verifier_plugin_content import (
    verify_plugin,
)
from omnimarket.nodes.node_release_candidate_verify_effect.handlers.verifier_pypi_ancestry import (
    verify_ancestry,
    verify_pypi,
)
from omnimarket.nodes.node_release_candidate_verify_effect.models.model_release_candidate_overlay import (
    ModelReleaseCandidateOverlay,
)
from omnimarket.nodes.node_release_candidate_verify_effect.models.model_release_candidate_verify_request import (
    ModelReleaseCandidateVerifyRequest,
)
from omnimarket.nodes.node_release_candidate_verify_effect.models.model_release_candidate_verify_result import (
    ModelInstalledDistribution,
    ModelReleaseCandidateFinding,
    ModelReleaseCandidateVerifyResult,
)


def load_default_overlay() -> ModelReleaseCandidateOverlay:
    text = (
        files("omnimarket.nodes.node_release_candidate_verify_effect")
        .joinpath("overlay.default.yaml")
        .read_text(encoding="utf-8")
    )
    return ModelReleaseCandidateOverlay.model_validate(yaml.safe_load(text))


class HandlerReleaseCandidateVerify:
    """A mismatch is a finding that names the file, entry point or launcher line; it never raises."""

    def __init__(self, overlay: ModelReleaseCandidateOverlay | None = None) -> None:
        self._overlay = overlay

    def handle(
        self, request: ModelReleaseCandidateVerifyRequest
    ) -> ModelReleaseCandidateVerifyResult:
        overlay = request.overlay or self._overlay or load_default_overlay()
        findings: list[ModelReleaseCandidateFinding] = []
        layout = ""
        closure: tuple[ModelInstalledDistribution, ...] = ()
        commit = ""
        if request.operation == "verify_installed":
            assert request.environment_prefix is not None
            findings, layout, closure = verify_installed(
                request.manifest,
                overlay,
                request.environment_prefix,
                request.onex_on_path,
            )
        elif request.operation == "verify_plugin":
            assert request.plugin_record is not None
            findings = verify_plugin(
                request.manifest.plugin, overlay, request.plugin_record
            )
            commit = request.plugin_record.git_commit_sha
        elif request.operation == "verify_pypi":
            findings = verify_pypi(request.manifest, request.pypi_facts)
        else:
            findings = verify_ancestry(request.manifest, request.ancestry_facts)
        matches = not findings
        return ModelReleaseCandidateVerifyResult(
            correlation_id=request.correlation_id,
            operation=request.operation,
            manifest_id=request.manifest.manifest_id,
            matches=matches,
            installed_matches_manifest=matches
            if request.operation == "verify_installed"
            else None,
            plugin_matches_manifest=matches
            if request.operation == "verify_plugin"
            else None,
            reading=_reading(request.operation, matches),
            layout=layout,
            installed_git_commit_sha=commit,
            installed_distributions=closure,
            findings=tuple(findings),
        )


def _reading(operation: str, matches: bool) -> str:
    if matches:
        return "candidate"
    return (
        "refused"
        if operation in {"verify_pypi", "verify_ancestry"}
        else "not-the-candidate"
    )
