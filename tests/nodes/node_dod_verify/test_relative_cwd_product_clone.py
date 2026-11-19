# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

from pathlib import Path

import pytest

from omnimarket.nodes.node_dod_verify.services.evidence_collector import (
    EvidenceCollector,
    _repo_contract_subject,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def hosted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[EvidenceCollector, Path, Path]:
    app = tmp_path / "app"
    clone = app / "product"
    (clone / "src").mkdir(parents=True)
    (app / "src").mkdir()
    monkeypatch.setenv("OMNI_HOME", str(app))
    monkeypatch.setenv("REPO", "Example/sibling")
    monkeypatch.delenv("CONTRACT_REPO_DIR", raising=False)
    monkeypatch.chdir(app)
    collector = EvidenceCollector()
    collector.contract_subject = _repo_contract_subject(
        "Example/product", "a" * 40, "OMN-TEST"
    )
    return collector, app, clone


@pytest.mark.parametrize("cwd", [".", "src"])
def test_relative_cwd_product_clone_uses_governing_repository(
    hosted: tuple[EvidenceCollector, Path, Path], cwd: str
) -> None:
    collector, _, clone = hosted
    assert collector._resolve_check_cwd({"cwd": cwd}, "OMN-TEST", None) == (
        str(clone / cwd),
        None,
        True,
    )


@pytest.mark.parametrize("cwd", ["../sibling", "$CHECK_CWD", "linked-product"])
def test_relative_cwd_product_clone_refuses_escape(
    hosted: tuple[EvidenceCollector, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    cwd: str,
) -> None:
    collector, app, clone = hosted
    sibling = app / "sibling"
    sibling.mkdir()
    (app / "linked-product").symlink_to(sibling, target_is_directory=True)
    (clone / "linked-product").symlink_to(sibling, target_is_directory=True)
    monkeypatch.setenv("CHECK_CWD", "../sibling")
    resolved, error, declared = collector._resolve_check_cwd(
        {"cwd": cwd}, "OMN-TEST", None
    )
    assert declared
    assert resolved is None
    assert error is not None
    assert "traversal" in error or "escapes" in error


def test_relative_cwd_product_clone_refuses_absolute_outside(
    hosted: tuple[EvidenceCollector, Path, Path],
) -> None:
    collector, app, _ = hosted
    resolved, error, _ = collector._resolve_check_cwd(
        {"cwd": str(app.parent)}, "OMN-TEST", None
    )
    assert resolved is None
    assert error is not None
    assert "escapes OMNI_HOME containment" in error


@pytest.mark.parametrize("replacement", ["absent", "file", "symlink"])
def test_relative_cwd_product_clone_requires_own_directory(
    hosted: tuple[EvidenceCollector, Path, Path], replacement: str
) -> None:
    collector, app, clone = hosted
    (clone / "src").rmdir()
    clone.rmdir()
    if replacement == "file":
        clone.touch()
    elif replacement == "symlink":
        clone.symlink_to(app / "src", target_is_directory=True)
    resolved, error, _ = collector._resolve_check_cwd({"cwd": "."}, "OMN-TEST", None)
    assert resolved is None
    assert error is not None
    assert "product clone" in error
