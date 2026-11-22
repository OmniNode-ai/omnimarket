# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The served curl installer against the candidate manifest (OMN-25070 AC4, -k installer_matches_manifest)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_onex_installer_render_compute.handlers.handler_onex_installer_render import (
    HandlerOnexInstallerRender,
)
from omnimarket.nodes.node_onex_installer_render_compute.models.model_onex_installer import (
    ModelOnexInstallerRequest,
)
from omnimarket.nodes.node_release_candidate_verify_effect.handlers.handler_release_candidate_verify import (
    HandlerReleaseCandidateVerify,
)
from omnimarket.nodes.node_release_candidate_verify_effect.models.enum_release_candidate_finding import (
    EnumReleaseCandidateFinding as K,
)
from omnimarket.nodes.node_release_candidate_verify_effect.models.model_release_candidate_manifest import (
    ModelManifestDistribution,
    ModelManifestInstaller,
    ModelManifestRecordEntry,
    ModelReleaseCandidateManifest,
)
from omnimarket.nodes.node_release_candidate_verify_effect.models.model_release_candidate_verify_request import (
    ModelReleaseCandidateVerifyRequest,
)
from omnimarket.nodes.node_release_candidate_verify_effect.models.model_release_candidate_verify_result import (
    ModelReleaseCandidateVerifyResult,
)

pytestmark = pytest.mark.unit

CANDIDATE: str = "0.0.1.post2"

_HANDLER: HandlerReleaseCandidateVerify = HandlerReleaseCandidateVerify()


def _render(version: str = CANDIDATE) -> str:
    config: dict[str, Any] = json.loads(
        (
            Path(__file__).resolve().parents[3]
            / "packages"
            / "omninode-onex"
            / "installer.json"
        ).read_text()
    )
    config["version"] = version
    request: ModelOnexInstallerRequest = ModelOnexInstallerRequest.model_validate(
        config
    )
    return HandlerOnexInstallerRender().handle(request).script


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _customer_distribution() -> ModelManifestDistribution:
    return ModelManifestDistribution(
        name="omninode-onex",
        version=CANDIDATE,
        wheel_filename=f"omninode_onex-{CANDIDATE}-py3-none-any.whl",
        wheel_sha256="b" * 64,
        record=(
            ModelManifestRecordEntry(path="omninode_onex/__init__.py", sha256="c" * 64),
        ),
    )


def _with_customer(
    manifest: ModelReleaseCandidateManifest, script: str
) -> ModelReleaseCandidateManifest:
    distributions: list[ModelManifestDistribution] = list(manifest.distributions)
    distributions.append(_customer_distribution())
    return manifest.model_copy(
        update={
            "distributions": tuple(distributions),
            "installer": ModelManifestInstaller(sha256=_sha(script)),
        }
    )


def _verify(
    manifest: ModelReleaseCandidateManifest, script: str
) -> ModelReleaseCandidateVerifyResult:
    request: ModelReleaseCandidateVerifyRequest = ModelReleaseCandidateVerifyRequest(
        operation="verify_installer",
        installer_script=script,
        manifest=manifest,
    )
    return _HANDLER.handle(request)


def _kinds(result: ModelReleaseCandidateVerifyResult) -> set[K]:
    return {finding.kind for finding in result.findings}


def test_installer_matches_manifest(manifest: ModelReleaseCandidateManifest) -> None:
    script: str = _render()
    assert "--no-build" in script
    assert f'ONEX_VERSION="{CANDIDATE}"' in script
    result: ModelReleaseCandidateVerifyResult = _verify(
        _with_customer(manifest, script), script
    )
    assert result.matches is True
    assert result.installer_matches_manifest is True
    assert result.reading == "candidate"
    assert result.findings == ()


def test_installer_matches_manifest_refuses_a_changed_script(
    manifest: ModelReleaseCandidateManifest,
) -> None:
    script: str = _render()
    served: str = script + "\n# changed\n"
    result: ModelReleaseCandidateVerifyResult = _verify(
        _with_customer(manifest, script), served
    )
    assert _kinds(result) == {K.INSTALLER_SHA256}
    assert result.matches is False
    assert result.installer_matches_manifest is False
    assert result.reading == "refused"


def test_installer_matches_manifest_refuses_another_version(
    manifest: ModelReleaseCandidateManifest,
) -> None:
    script: str = _render(version="0.0.1.post1")
    result: ModelReleaseCandidateVerifyResult = _verify(
        _with_customer(manifest, script), script
    )
    kinds: set[K] = _kinds(result)
    assert K.INSTALLER_VERSION in kinds
    assert K.INSTALLER_SHA256 not in kinds
    details: list[str] = [finding.detail for finding in result.findings]
    assert any("0.0.1.post1" in detail and CANDIDATE in detail for detail in details)


@pytest.mark.parametrize(
    "edit",
    [
        " --index-url https://packages.example.invalid/simple",
        " --extra-index-url https://packages.example.invalid/simple",
        " --default-index https://packages.example.invalid/simple",
        "export UV_INDEX_URL=https://packages.example.invalid/simple",
        "export UV_INDEX=https://packages.example.invalid/simple",
        "export PIP_INDEX_URL=https://packages.example.invalid/simple",
    ],
)
def test_installer_matches_manifest_refuses_another_index(
    manifest: ModelReleaseCandidateManifest, edit: str
) -> None:
    script: str = _render()
    if edit.startswith(" "):
        anchor: str = '    set -- tool install --python "$ONEX_PYTHON" --no-build'
        assert script.count(anchor) == 1
        edited: str = script.replace(anchor, anchor + edit, 1)
    else:
        anchor = "set -euo pipefail"
        assert script.count(anchor) == 1
        edited = script.replace(anchor, anchor + "\n" + edit, 1)
    result: ModelReleaseCandidateVerifyResult = _verify(
        _with_customer(manifest, edited), edited
    )
    assert _kinds(result) == {K.INSTALLER_INDEX}
    details: list[str] = [finding.detail for finding in result.findings]
    assert any("packages.example.invalid" in detail for detail in details)


def test_installer_matches_manifest_allows_pypi_named_explicitly(
    manifest: ModelReleaseCandidateManifest,
) -> None:
    script: str = _render()
    anchor: str = '    set -- tool install --python "$ONEX_PYTHON" --no-build'
    assert script.count(anchor) == 1
    edited: str = script.replace(
        anchor, anchor + " --index-url https://pypi.org/simple", 1
    )
    result: ModelReleaseCandidateVerifyResult = _verify(
        _with_customer(manifest, edited), edited
    )
    assert result.matches is True


@pytest.mark.parametrize(
    "edit",
    [
        "remove_no_build",
        "insert_pip_install",
    ],
)
def test_installer_matches_manifest_refuses_an_install_without_no_build(
    manifest: ModelReleaseCandidateManifest, edit: str
) -> None:
    script: str = _render()
    if edit == "remove_no_build":
        anchor: str = '    set -- tool install --python "$ONEX_PYTHON" --no-build'
        assert script.count(anchor) == 1
        edited: str = script.replace(
            anchor, '    set -- tool install --python "$ONEX_PYTHON"', 1
        )
    else:
        anchor = '    set -- tool install --python "$ONEX_PYTHON" --no-build'
        assert script.count(anchor) == 1
        edited = script.replace(
            anchor, '    "$uv" pip install "$ONEX_PACKAGE==$ONEX_VERSION"\n' + anchor, 1
        )
    result: ModelReleaseCandidateVerifyResult = _verify(
        _with_customer(manifest, edited), edited
    )
    assert _kinds(result) == {K.INSTALLER_BUILD}


def test_installer_matches_manifest_refuses_another_package(
    manifest: ModelReleaseCandidateManifest,
) -> None:
    script: str = _render()
    anchor: str = 'ONEX_PACKAGE="omninode-onex"'
    assert script.count(anchor) == 1
    edited: str = script.replace(anchor, 'ONEX_PACKAGE="omninode-onex-fork"', 1)
    result: ModelReleaseCandidateVerifyResult = _verify(
        _with_customer(manifest, edited), edited
    )
    assert K.INSTALLER_PACKAGE in _kinds(result)


def test_installer_matches_manifest_needs_the_manifest_to_name_an_installer(
    manifest: ModelReleaseCandidateManifest,
) -> None:
    script: str = _render()
    distributions: list[ModelManifestDistribution] = list(manifest.distributions)
    distributions.append(_customer_distribution())
    customer: ModelReleaseCandidateManifest = manifest.model_copy(
        update={"distributions": tuple(distributions), "installer": None}
    )
    result: ModelReleaseCandidateVerifyResult = _verify(customer, script)
    assert _kinds(result) == {K.INSTALLER_MISSING_FROM_MANIFEST}


def test_installer_matches_manifest_needs_a_script(
    manifest: ModelReleaseCandidateManifest,
) -> None:
    with pytest.raises(
        ValidationError, match="verify_installer needs installer_script"
    ):
        ModelReleaseCandidateVerifyRequest(
            operation="verify_installer",
            installer_script=None,
            manifest=manifest,
        )
