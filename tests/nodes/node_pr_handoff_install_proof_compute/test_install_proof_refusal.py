# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Error chain: a missing or invalid overlay refuses every request (OMN-23616)."""

from __future__ import annotations

import os
import tempfile

import yaml

from omnimarket.handlers.node_overlay_reader import NodeOverlayNotFoundError
from omnimarket.models.pr_handoff_install_proof import (
    ModelInstallDeclaration,
    ModelInstallProofRequest,
)
from omnimarket.nodes.node_pr_handoff_install_proof_compute.handlers.handler_install_proof import (
    HandlerInstallProof,
)
from omnimarket.nodes.node_pr_handoff_install_proof_compute.overlay import (
    NODE_NAME,
    load_install_declaration,
)


def _ci_request() -> ModelInstallProofRequest:
    return ModelInstallProofRequest(
        repo="repo-a", head="abc1234", files=("scripts/ci/check_x.py",)
    )


def test_loader_raises_refuses_every_request() -> None:
    def _raise() -> ModelInstallDeclaration:
        raise NodeOverlayNotFoundError(f"{NODE_NAME}: no overlay found")

    handler = HandlerInstallProof(declaration_loader=_raise)
    result = handler.handle(_ci_request())
    assert result.verdict == "REFUSED"
    assert "NodeOverlayNotFoundError" in result.detail


def test_empty_overlay_root_refuses() -> None:
    with tempfile.TemporaryDirectory() as root:
        env = {"ONEX_SKILL_OVERLAY_ROOTS": root}
        handler = HandlerInstallProof(
            declaration_loader=lambda: load_install_declaration(env)
        )
        result = handler.handle(_ci_request())
        assert result.verdict == "REFUSED"


def test_unknown_key_refuses() -> None:
    with tempfile.TemporaryDirectory() as root:
        overlay_path = os.path.join(root, NODE_NAME, "overlay.yaml")
        os.makedirs(os.path.dirname(overlay_path))
        with open(overlay_path, "w") as fh:
            yaml.safe_dump({"not_a_real_key": "value"}, fh)
        env = {"ONEX_SKILL_OVERLAY_ROOTS": root}
        handler = HandlerInstallProof(
            declaration_loader=lambda: load_install_declaration(env)
        )
        result = handler.handle(_ci_request())
        assert result.verdict == "REFUSED"


def test_failing_control_excluded_patterns() -> None:
    """A declaration without the four new excluded patterns returns NO for a CI script;

    the real one returns NA, so the test fails without the change.
    """
    from omnimarket.nodes.node_pr_handoff_install_proof_compute.handlers.handler_install_proof import (
        install_files,
    )

    full = ModelInstallDeclaration(
        paths=("scripts/*", "*/scripts/*"),
        repo_paths={},
        excluded_paths=("scripts/ci/*", "*/scripts/ci/*"),
        prod_paths=(),
        installed_cell="installed",
        observed_cell="observed",
        observed_words=("ran",),
        observed_refused=(),
        min_observed_chars=1,
        ruling="r",
    )
    assert install_files(("scripts/ci/check_x.py",), "repo-a", full) == ()

    without = ModelInstallDeclaration(
        paths=("scripts/*", "*/scripts/*"),
        repo_paths={},
        excluded_paths=(),
        prod_paths=(),
        installed_cell="installed",
        observed_cell="observed",
        observed_words=("ran",),
        observed_refused=(),
        min_observed_chars=1,
        ruling="r",
    )
    assert install_files(("scripts/ci/check_x.py",), "repo-a", without) == (
        "scripts/ci/check_x.py",
    )
