# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chains of the release candidate verifier (OMN-23343).

The chain is the one the contract declares: a verify command arrives as JSON, the handler returns the
typed result, and the result serialises onto the declared terminal event topic.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_release_candidate_verify_effect.handlers.handler_release_candidate_verify import (
    HandlerReleaseCandidateVerify,
    load_default_overlay,
)
from omnimarket.nodes.node_release_candidate_verify_effect.models.model_release_candidate_manifest import (
    ModelReleaseCandidateManifest,
)
from omnimarket.nodes.node_release_candidate_verify_effect.models.model_release_candidate_verify_request import (
    ModelReleaseCandidateVerifyRequest,
)
from omnimarket.nodes.node_release_candidate_verify_effect.models.model_release_candidate_verify_result import (
    ModelReleaseCandidateVerifyResult,
)
from tests.nodes.node_release_candidate_verify_effect import support

pytestmark = pytest.mark.unit

NODE_DIR = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_release_candidate_verify_effect"
)


def _contract() -> dict[str, Any]:
    loaded = yaml.safe_load((NODE_DIR / "contract.yaml").read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _chain(command_json: str) -> ModelReleaseCandidateVerifyResult:
    contract = _contract()
    assert (
        contract["terminal_event"]
        == "onex.evt.omnimarket.release-candidate-verified.v1"
    )
    assert contract["terminal_event"] in contract["event_bus"]["publish_topics"]
    assert (
        contract["runtime_dispatch"]["command_topic"]
        in contract["event_bus"]["subscribe_topics"]
    )
    result = HandlerReleaseCandidateVerify().handle(
        ModelReleaseCandidateVerifyRequest.model_validate_json(command_json)
    )
    return ModelReleaseCandidateVerifyResult.model_validate_json(
        result.model_dump_json()
    )


def test_golden_chain_untouched_install_reaches_the_terminal_event(
    manifest: ModelReleaseCandidateManifest, wheel_dir: Path, tmp_path: Path
) -> None:
    prefix, onex = support.install_venv(tmp_path, wheel_dir, with_pip=False)
    event = _chain(support.request(manifest, prefix, onex).model_dump_json())
    assert event.installed_matches_manifest is True
    assert event.findings == ()


def test_error_chain_edited_install_reaches_the_terminal_event(
    manifest: ModelReleaseCandidateManifest, wheel_dir: Path, tmp_path: Path
) -> None:
    prefix, onex = support.install_venv(tmp_path, wheel_dir, with_pip=False)
    module = next(
        Path(prefix).glob("lib/python*/site-packages/omnibase_core/cli/cli_commands.py")
    )
    module.write_text(module.read_text() + "#")
    event = _chain(support.request(manifest, prefix, onex).model_dump_json())
    assert event.installed_matches_manifest is False
    assert event.findings[0].subject == "omnibase_core/cli/cli_commands.py"


def test_unreadable_environment_is_a_finding_not_a_crash(
    manifest: ModelReleaseCandidateManifest, tmp_path: Path
) -> None:
    event = _chain(
        support.request(manifest, str(tmp_path / "absent"), "").model_dump_json()
    )
    assert event.matches is False
    assert event.findings[0].kind == "environment_unreadable"


def test_request_without_its_operation_inputs_is_refused(
    manifest: ModelReleaseCandidateManifest,
) -> None:
    with pytest.raises(ValueError, match="needs plugin_record"):
        ModelReleaseCandidateVerifyRequest(operation="verify_plugin", manifest=manifest)


def test_overlay_carries_the_four_admitted_launcher_templates() -> None:
    overlay = load_default_overlay()
    assert [t.name for t in overlay.launcher_templates] == [
        "uv",
        "uv-long-path",
        "pip",
        "pip-long-path",
        "pip-26",
        "pip-26-long-path",
    ]
    assert all(t.installer_version for t in overlay.launcher_templates)
