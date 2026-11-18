# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Build-lock facts for the dispatch venv (OMN-19998)."""

import hashlib
from pathlib import Path

import pytest

from tests.test_host_reconcile_effect import (
    RecordingEvaluator,
    RecordingPublisher,
    command,
    handler,
    setup_workspace,
)

pytestmark = pytest.mark.unit

LOCK_V2 = (
    "version = 1\n"
    "[[package]]\n"
    'name = "omnibase-core"\n'
    'version = "2"\n'
    'source = { registry = "https://pypi.org/simple" }\n'
)
LOCK_V3 = (
    "version = 1\n"
    "[[package]]\n"
    'name = "omnibase-core"\n'
    'version = "3"\n'
    'source = { registry = "https://pypi.org/simple" }\n'
)
LOCK_V5 = (
    "version = 1\n"
    "[[package]]\n"
    'name = "omnibase-core"\n'
    'version = "5"\n'
    'source = { registry = "https://pypi.org/simple" }\n'
)


def stamp_marker(venv: Path, lock_text: str, *, sha256: str | None = None) -> None:
    copy = venv / ".built-from.uv.lock"
    copy.write_text(lock_text, encoding="utf-8")
    digest = (
        sha256
        if sha256 is not None
        else hashlib.sha256(lock_text.encode("utf-8")).hexdigest()
    )
    (venv / ".built-from").write_text(
        f"lock_path={copy}\nlock_sha256={digest}\nlock_copy=.built-from.uv.lock\n",
        encoding="utf-8",
    )


def _dispatch_venv(root: Path) -> Path:
    return root / ".onex-dispatch-venv"


def _surface(result, surface: str):
    return next((s for s in result.surfaces if s.surface == surface), None)


def test_golden_chain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = setup_workspace(tmp_path, monkeypatch)
    venv = _dispatch_venv(root)
    (root / "omnibase_infra/uv.lock").write_text(LOCK_V2, encoding="utf-8")
    stamp_marker(venv, LOCK_V2)
    evaluator = RecordingEvaluator()
    result = handler(evaluator, RecordingPublisher()).handle(command(root, "check"))
    request = evaluator.requests[-1]
    fact = next(f for f in request.facts if f.kind == "venv_build_lock")
    assert fact.valid is True
    assert fact.surfaces_judged == 1
    assert not any(issue.code.startswith("build_lock") for issue in fact.issues)
    surface = _surface(result, "venv:build-lock")
    assert surface is not None
    assert surface.verdict == "ALREADY_AT_TARGET"
    assert not any(s.verdict == "LOCK_MISMATCH" for s in result.surfaces)


def test_error_chain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = setup_workspace(tmp_path, monkeypatch)
    venv = _dispatch_venv(root)
    (root / "omnibase_infra/uv.lock").write_text(LOCK_V2, encoding="utf-8")
    stamp_marker(venv, LOCK_V3)
    evaluator = RecordingEvaluator()
    result = handler(evaluator, RecordingPublisher()).handle(command(root, "check"))
    surface = _surface(result, "venv:build-lock")
    assert surface is not None
    assert surface.verdict == "LOCK_MISMATCH"
    assert result.exit_code == 2
    assert "omnibase-core" in surface.detail
    assert "pins 3" in surface.detail
    fact = next(f for f in evaluator.requests[-1].facts if f.kind == "venv_build_lock")
    assert any(issue.code == "checkout_lock_not_authority" for issue in fact.issues)


def test_declared_lock_differs_but_venv_matches_build_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = setup_workspace(tmp_path, monkeypatch)
    venv = _dispatch_venv(root)
    (root / "omnibase_infra/uv.lock").write_text(LOCK_V5, encoding="utf-8")
    stamp_marker(venv, LOCK_V2)
    evaluator = RecordingEvaluator()
    result = handler(evaluator, RecordingPublisher()).handle(command(root, "check"))
    surface = _surface(result, "venv:build-lock")
    assert surface is not None
    assert surface.verdict == "ALREADY_AT_TARGET"
    assert "checkout" in surface.detail.lower()


def test_torn_marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = setup_workspace(tmp_path, monkeypatch)
    venv = _dispatch_venv(root)
    (root / "omnibase_infra/uv.lock").write_text(LOCK_V2, encoding="utf-8")
    stamp_marker(venv, LOCK_V2, sha256="0" * 64)
    evaluator = RecordingEvaluator()
    result = handler(evaluator, RecordingPublisher()).handle(command(root, "check"))
    surface = _surface(result, "venv:build-lock")
    assert surface is not None
    assert surface.verdict == "UNHEALTHY"
    assert result.exit_code == 2


def test_no_marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = setup_workspace(tmp_path, monkeypatch)
    (root / "omnibase_infra/uv.lock").write_text(LOCK_V2, encoding="utf-8")
    evaluator = RecordingEvaluator()
    result = handler(evaluator, RecordingPublisher()).handle(command(root, "check"))
    assert not any(s.surface == "venv:build-lock" for s in result.surfaces)
    assert any(
        "no .built-from marker" in line and "skipped" in line
        for line in result.diagnostics
    )


def test_declared_lock_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = setup_workspace(tmp_path, monkeypatch)
    venv = _dispatch_venv(root)
    stamp_marker(venv, LOCK_V2)
    evaluator = RecordingEvaluator()
    result = handler(evaluator, RecordingPublisher()).handle(command(root, "check"))
    surface = _surface(result, "venv:build-lock")
    assert surface is not None
    assert surface.verdict == "INDETERMINATE"
    assert result.exit_code == 2


def test_seam_marker_as_the_writer_spells_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Seam with omnibase_infra promote-floor: the marker is read by its literal keys.

    The writer (dispatch_venv_promotion.stamp_build_lock) emits exactly three lines
    in this order and a lock copy named .built-from.uv.lock. The text below is spelled
    here on purpose: this test must not take the names from the reader it checks.
    """
    root = setup_workspace(tmp_path, monkeypatch)
    venv = _dispatch_venv(root)
    (root / "omnibase_infra/uv.lock").write_text(LOCK_V2, encoding="utf-8")
    (venv / ".built-from.uv.lock").write_text(LOCK_V2, encoding="utf-8")
    sha = hashlib.sha256(LOCK_V2.encode("utf-8")).hexdigest()
    (venv / ".built-from").write_text(
        f"lock_path={root}/omnibase_infra/uv.lock\n"
        f"lock_sha256={sha}\n"
        "lock_copy=.built-from.uv.lock\n",
        encoding="utf-8",
    )
    result = handler(RecordingEvaluator(), RecordingPublisher()).handle(
        command(root, "check")
    )
    surface = _surface(result, "venv:build-lock")
    assert surface is not None
    assert surface.verdict == "ALREADY_AT_TARGET"
    assert sha[:12] in surface.detail


def test_seam_renamed_marker_key_is_reported_not_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failing control for the seam: if the writer renames lock_sha256 the reader says so."""
    root = setup_workspace(tmp_path, monkeypatch)
    venv = _dispatch_venv(root)
    (root / "omnibase_infra/uv.lock").write_text(LOCK_V2, encoding="utf-8")
    (venv / ".built-from.uv.lock").write_text(LOCK_V2, encoding="utf-8")
    sha = hashlib.sha256(LOCK_V2.encode("utf-8")).hexdigest()
    (venv / ".built-from").write_text(
        f"lock_path=x\nlock_hash={sha}\nlock_copy=.built-from.uv.lock\n",
        encoding="utf-8",
    )
    result = handler(RecordingEvaluator(), RecordingPublisher()).handle(
        command(root, "check")
    )
    surface = _surface(result, "venv:build-lock")
    assert surface is not None
    assert surface.verdict == "UNHEALTHY"
    assert "lock_sha256" in surface.detail
    assert result.exit_code == 2


def test_lock_mismatch_alone_does_not_block_the_floor_stamp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = setup_workspace(tmp_path, monkeypatch)
    venv = _dispatch_venv(root)
    (root / "omnibase_infra/uv.lock").write_text(LOCK_V2, encoding="utf-8")
    stamp_marker(venv, LOCK_V3)
    result = handler(RecordingEvaluator(), RecordingPublisher()).handle(command(root))
    surface = _surface(result, "venv:build-lock")
    assert surface is not None
    assert surface.verdict == "LOCK_MISMATCH"
    assert result.exit_code == 2
    assert result.floor_stamped
