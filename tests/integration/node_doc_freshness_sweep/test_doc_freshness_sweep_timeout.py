# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Reproducing tests for OMN-13716 — doc_freshness_sweep: no-scope full-repo
scan hangs >9min when invoked without --repos.

Before the fix: no ``per_repo_timeout_s`` field existed; scanning all 11 default
repos could hang indefinitely.

After the fix:
  - ``DocFreshnessSweepRequest`` has ``per_repo_timeout_s: float | None`` (default 30s).
  - Each repo is scanned in a thread; when it exceeds the timeout, it is skipped
    with a warning and the scan continues to the next repo — completing in bounded
    time.
  - Passing ``per_repo_timeout_s=None`` disables the guard for explicit scoped calls.
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest import mock

import pytest

from omnimarket.nodes.node_doc_freshness_sweep.handlers.handler_doc_freshness_sweep import (
    _OCC_AVAILABLE,
    DocFreshnessSweepRequest,
    NodeDocFreshnessSweep,
)

pytestmark = pytest.mark.skipif(
    not _OCC_AVAILABLE,
    reason="onex_change_control not installed — skipping rather than faking.",
)


@pytest.mark.unit
class TestDocFreshnessSweepTimeoutField:
    """The new per_repo_timeout_s field must be present with a safe default."""

    def test_default_timeout_is_set(self) -> None:
        req = DocFreshnessSweepRequest()
        assert req.per_repo_timeout_s is not None
        assert req.per_repo_timeout_s > 0

    def test_timeout_can_be_disabled(self) -> None:
        req = DocFreshnessSweepRequest(per_repo_timeout_s=None)
        assert req.per_repo_timeout_s is None

    def test_explicit_timeout_accepted(self) -> None:
        req = DocFreshnessSweepRequest(per_repo_timeout_s=5.0)
        assert req.per_repo_timeout_s == 5.0


@pytest.mark.integration
def test_doc_freshness_sweep_respects_per_repo_timeout(tmp_path: Path) -> None:
    """A repo whose scan exceeds per_repo_timeout_s must be skipped; the overall
    call must complete within bounded time (not hang).

    Mechanism: we patch _scan_repo_docs to simulate a very slow repo scan by
    sleeping for longer than the configured timeout.  The outer handler must
    time it out, skip that repo, and still return a valid DocFreshnessSweepResult.
    """
    import omnimarket.nodes.node_doc_freshness_sweep.handlers.handler_doc_freshness_sweep as mod

    # Build two fake repos: one slow (will be timed out), one fast (will succeed).
    slow_root = tmp_path / "omni_home" / "slow_repo"
    fast_root = tmp_path / "omni_home" / "fast_repo"
    for root in (slow_root, fast_root):
        root.mkdir(parents=True)
        (root / "README.md").write_text("# Hello\n", encoding="utf-8")

    def _slow_scan(repo: str, *args: object, **kwargs: object) -> list[object]:
        if repo == "slow_repo":
            time.sleep(10)  # far exceeds the 0.5s timeout below
        return []  # fast_repo returns immediately

    with mock.patch.object(mod, "_scan_repo_docs", side_effect=_slow_scan):
        request = DocFreshnessSweepRequest(
            omni_home=str(tmp_path / "omni_home"),
            repos=["slow_repo", "fast_repo"],
            dry_run=True,
            per_repo_timeout_s=0.5,  # tight timeout to make the test fast
        )
        start = time.monotonic()
        result = NodeDocFreshnessSweep().handle(request)
        elapsed = time.monotonic() - start

    # Must complete in well under the slow_repo's sleep duration
    assert elapsed < 5.0, f"Scan took {elapsed:.1f}s — timeout not enforced"
    # slow_repo was timed out → not in repos_scanned
    assert "slow_repo" not in result.repos_scanned
    # fast_repo finished before timeout (but _scan_repo_docs returns [] so no docs)
    # The handler still records it as scanned when there are md files.
    # Because our mock returns [] for fast_repo AND _collect_md_files returns
    # real files, fast_repo is added to repos_scanned.
    assert "fast_repo" in result.repos_scanned


@pytest.mark.integration
def test_doc_freshness_sweep_scoped_completes_fast(tmp_path: Path) -> None:
    """Scoped run (repos=[one]) with a real single-repo fixture must complete.

    This mirrors the known-working evidence: --repos omnibase_compat → 0.7s.
    We verify that the timeout path works correctly for the scoped (fast) case.
    """
    omni_home = tmp_path / "omni_home"
    repo_root = omni_home / "myrepo"
    repo_root.mkdir(parents=True)
    (repo_root / "README.md").write_text(
        "# Test\nSee `src/real.py`.\n", encoding="utf-8"
    )
    (repo_root / "src").mkdir()
    (repo_root / "src" / "real.py").write_text("x = 1\n", encoding="utf-8")

    request = DocFreshnessSweepRequest(
        omni_home=str(omni_home),
        repos=["myrepo"],
        dry_run=True,
        per_repo_timeout_s=30.0,
    )
    start = time.monotonic()
    result = NodeDocFreshnessSweep().handle(request)
    elapsed = time.monotonic() - start

    assert elapsed < 10.0, f"Scoped scan took {elapsed:.1f}s — unexpectedly slow"
    assert result.repos_scanned == ["myrepo"]
    assert result.total_docs >= 1
