# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-17427: select the rebuild publisher that requires an amd64 lab runner.

omnibase_infra#5369 added arch-amd64 to the publisher's default runner labels.
The older cb5c0cfd pin can select h203-light, where CI environment setup fails
before the publisher emits a rebuild command or a lab receipt. The selected
descendant also reads the lane overlay using the caller's OCC app credentials.
"""

from pathlib import Path
from typing import Any, cast

import pytest
import yaml

pytestmark = pytest.mark.unit

_WORKFLOW = (
    Path(__file__).resolve().parents[2]
    / ".github/workflows/runtime-rebuild-trigger.yml"
)
_REUSABLE = (
    "OmniNode-ai/omnibase_infra/.github/workflows/runtime-rebuild-trigger-reusable.yml"
)
_AMD64_PIN = "dev-20261028175438-79bc8a26389c"


def _publisher() -> dict[str, Any]:
    workflow = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    return cast("dict[str, Any]", workflow["jobs"]["trigger-rebuild"])


def test_rebuild_publisher_selects_the_amd64_runner_revision() -> None:
    assert _publisher()["uses"] == f"{_REUSABLE}@{_AMD64_PIN}"


def test_deploy_caller_passes_merge_identity_and_overlay_credentials() -> None:
    """The pinned publisher must receive the merged revision and overlay auth."""
    publisher = _publisher()
    inputs = publisher["with"]
    assert inputs["source_repo"] == "omnimarket"
    assert inputs["repo_slug"] == "OmniNode-ai/omnimarket"
    assert inputs["source_sha"] == "${{ github.event.pull_request.merge_commit_sha }}"
    assert inputs["base_branch"] == "${{ github.event.pull_request.base.ref }}"
    assert inputs["changed_files"] == "${{ needs.changed-files.outputs.all }}"
    assert "infra_ref" not in inputs
    for secret in (
        "KAFKA_SASL_USERNAME",
        "KAFKA_SASL_PASSWORD",
        "ONEXBOT_OCC_APP_ID",
        "ONEXBOT_OCC_PRIVATE_KEY",
    ):
        assert publisher["secrets"][secret] == "${{ secrets." + secret + " }}"
