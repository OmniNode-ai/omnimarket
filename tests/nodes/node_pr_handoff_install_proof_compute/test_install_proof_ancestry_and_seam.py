# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ancestry chain and the overlay seam of the install-proof node (OMN-23616)."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import pytest
import yaml

from omnimarket.models.pr_handoff_install_proof import (
    ModelInstallAncestry,
    ModelInstallDeclaration,
    ModelInstallProofRequest,
    ModelInstallProofSource,
)
from omnimarket.nodes.node_pr_handoff_install_proof_compute import overlay
from omnimarket.nodes.node_pr_handoff_install_proof_compute.handlers.handler_install_proof import (
    HandlerInstallProof,
)

NODE_DIR = Path(overlay.__file__).parent
HEAD = "a" * 40
MERGE = "b" * 40
LAB = f"Lab: host=h201 installed=h201:{MERGE[:12]} observed=the unit ran and printed output"


@pytest.fixture
def declaration() -> ModelInstallDeclaration:
    return ModelInstallDeclaration.model_validate(
        yaml.safe_load((NODE_DIR / "overlay.example.yaml").read_text(encoding="utf-8"))
    )


def _request(
    verdict: Literal["yes", "no", "unread"] | None,
) -> ModelInstallProofRequest:
    answers = (
        ()
        if verdict is None
        else (
            ModelInstallAncestry(rev=MERGE[:12], head=HEAD, verdict=verdict, why="x"),
        )
    )
    return ModelInstallProofRequest(
        repo="repo-a",
        head=HEAD,
        files=("launchd/ai.example.plist",),
        sources=(ModelInstallProofSource(name="body", text=LAB),),
        ancestry=answers,
    )


def test_golden_installed_revision_that_contains_the_head_is_ok(
    declaration: ModelInstallDeclaration,
) -> None:
    result = HandlerInstallProof(lambda: declaration).handle(_request("yes"))

    assert result.verdict == "OK"
    assert "contains-head=" + HEAD[:7] in result.detail


@pytest.mark.parametrize("verdict", ["no", "unread", None])
def test_error_chain_a_revision_that_does_not_contain_the_head_never_passes(
    declaration: ModelInstallDeclaration,
    verdict: Literal["yes", "no", "unread"] | None,
) -> None:
    result = HandlerInstallProof(lambda: declaration).handle(_request(verdict))

    assert result.verdict == "NO"
    assert result.required_files == ("launchd/ai.example.plist",)


def test_error_chain_tests_passing_is_not_an_observation(
    declaration: ModelInstallDeclaration,
) -> None:
    text = f"Lab: head={HEAD[:7]} installed=h201:{HEAD[:7]} observed=all focused tests passed"
    request = ModelInstallProofRequest(
        repo="r",
        head=HEAD,
        files=("hooks/x.sh",),
        sources=(ModelInstallProofSource(name="body", text=text),),
    )

    assert HandlerInstallProof(lambda: declaration).handle(request).verdict == "NO"


def test_seam_the_example_overlay_holds_exactly_the_model_fields() -> None:
    keys = set(
        yaml.safe_load((NODE_DIR / "overlay.example.yaml").read_text(encoding="utf-8"))
    )

    assert keys == set(ModelInstallDeclaration.model_fields)


def test_seam_the_overlay_loader_reads_a_file_under_a_named_root(
    tmp_path: Path,
) -> None:
    folder = tmp_path / overlay.NODE_NAME
    folder.mkdir()
    (folder / "overlay.yaml").write_text(
        (NODE_DIR / "overlay.example.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    loaded = overlay.load_install_declaration(
        {"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)}
    )

    assert loaded.paths
    assert loaded.min_observed_chars >= 1


def test_seam_the_contract_declares_the_decided_terminal_topic() -> None:
    contract_path = (
        Path(__file__).resolve().parents[3]
        / "src/omnimarket/nodes/node_pr_handoff_install_proof_compute/contract.yaml"
    )
    contract = yaml.safe_load(contract_path.read_text())
    decided = "onex.evt.omnimarket.pr-handoff-install-proof-decided.v1"
    assert decided in contract["event_bus"]["publish_topics"]
    assert contract["event_bus"]["terminal_event"] == decided
