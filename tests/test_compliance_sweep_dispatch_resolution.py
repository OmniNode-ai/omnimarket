"""Regression tests for node_compliance_sweep dispatch-path target resolution.

Guards OMN-13514: the `onex skill compliance_sweep` (RuntimeLocal dispatch)
path was false-clean — it scanned 0 handlers and reported `compliant` because:

1. The handler request model exposed only ``target_dirs`` while the skill
   mapping / contract supplied ``repos``; with ``extra="forbid"`` a ``repos``
   payload was rejected and a no-arg payload defaulted ``target_dirs`` to ``[]``.
2. The default-repo list and repo-name -> absolute-path resolution lived only
   in ``__main__.py``, which the dispatch path never executes.

These tests assert the handler itself resolves the default repo set so a
no-arg dispatch scans the real handler universe, and that the scan scope
excludes worktree copies and test fixtures.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from omnimarket.nodes.node_compliance_sweep.handlers.handler_compliance_sweep import (
    _DEFAULT_REPOS,
    ComplianceSweepRequest,
    NodeComplianceSweep,
    resolve_target_dirs,
)


@pytest.mark.unit
class TestDispatchPathResolution:
    """The dispatch path (empty/`repos` payload) must scan real handlers."""

    def test_request_accepts_repos_field(self) -> None:
        """The skill mapping supplies ``repos`` — the model must accept it.

        Pre-fix: ``ComplianceSweepRequest(repos=[...])`` raised ValidationError
        because the field did not exist and ``extra="forbid"`` rejected it.
        """
        request = ComplianceSweepRequest(repos=["omnibase_core"])
        assert request.repos == ["omnibase_core"]

    def test_no_arg_request_scans_real_handlers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A no-arg dispatch (empty payload) must scan the default repo set.

        This is the core OMN-13514 regression: the skill returned
        ``handlers_scanned=0, status=compliant`` for a no-arg invocation.

        Hermetic: a synthetic ``$OMNI_HOME`` is populated with every default
        repo dir, each carrying one handler file, so the test does not depend
        on the live multi-repo workspace (which CI does not check out).
        """
        with tempfile.TemporaryDirectory() as tmp:
            omni_home = Path(tmp)
            for repo in _DEFAULT_REPOS:
                handlers = omni_home / repo / "src" / "nodes" / "node_a" / "handlers"
                handlers.mkdir(parents=True)
                (handlers / "handler_a.py").write_text("x = 1\n")
            monkeypatch.setenv("OMNI_HOME", str(omni_home))

            # Empty request — exactly what `onex skill compliance_sweep` (no
            # flags) produces through the RuntimeLocal dispatch path.
            result = NodeComplianceSweep().handle(ComplianceSweepRequest())

        assert result.handlers_scanned == len(_DEFAULT_REPOS), (
            "no-arg dispatch must resolve the default repo set and scan the "
            "real handler universe, not silently scan zero handlers"
        )

    def test_no_arg_request_fails_fast_without_omni_home(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unset ``$OMNI_HOME`` must fail fast, never silently scan zero."""
        monkeypatch.delenv("OMNI_HOME", raising=False)
        with pytest.raises(ValueError, match="OMNI_HOME is not set"):
            NodeComplianceSweep().handle(ComplianceSweepRequest())

    def test_repos_request_scans_resolved_handlers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A ``repos`` payload (bare names) must resolve against $OMNI_HOME."""
        with tempfile.TemporaryDirectory() as tmp:
            omni_home = Path(tmp)
            handlers = omni_home / "omnibase_core" / "handlers"
            handlers.mkdir(parents=True)
            (handlers / "handler_x.py").write_text("x = 1\n")
            monkeypatch.setenv("OMNI_HOME", str(omni_home))

            result = NodeComplianceSweep().handle(
                ComplianceSweepRequest(repos=["omnibase_core"])
            )
        assert result.handlers_scanned == 1

    def test_resolver_uses_default_repos_when_empty(self) -> None:
        """resolve_target_dirs falls back to _DEFAULT_REPOS for an empty request."""
        # Build a synthetic omni_home containing one default repo dir so the
        # test does not depend on the live workspace layout.
        with tempfile.TemporaryDirectory() as tmp:
            omni_home = Path(tmp)
            (omni_home / _DEFAULT_REPOS[0]).mkdir()
            request = ComplianceSweepRequest()
            resolved = resolve_target_dirs(request, omni_home)
            assert resolved, "default-repo fallback must resolve at least one dir"
            for d in resolved:
                assert Path(d).is_absolute()
                assert Path(d).is_dir()

    def test_resolver_prefers_explicit_target_dirs(self) -> None:
        """Explicit absolute target_dirs bypass repo-name resolution."""
        with tempfile.TemporaryDirectory() as tmp:
            request = ComplianceSweepRequest(target_dirs=[tmp])
            omni_home = Path("/nonexistent-omni-home")
            resolved = resolve_target_dirs(request, omni_home)
            assert resolved == [tmp]

    def test_default_repos_nonempty(self) -> None:
        """The shared default-repo list must be populated (off the __main__ path)."""
        assert len(_DEFAULT_REPOS) >= 1


@pytest.mark.unit
class TestScanScopeExclusion:
    """Scan scope must exclude worktree copies and test fixtures (OMN-13514)."""

    def test_excludes_worktrees(self) -> None:
        """Handlers under omni_worktrees/ must not be counted."""
        handler = NodeComplianceSweep()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            # A real handler under src/ and a duplicate under omni_worktrees/.
            real = root / "src" / "nodes" / "node_x" / "handlers"
            real.mkdir(parents=True)
            (real / "handler_real.py").write_text("x = 1\n")
            dup = root / "omni_worktrees" / "T" / "src" / "handlers"
            dup.mkdir(parents=True)
            (dup / "handler_dup.py").write_text("x = 1\n")

            result = handler.handle(ComplianceSweepRequest(target_dirs=[tmpdir]))
            assert result.handlers_scanned == 1

    def test_excludes_tests(self) -> None:
        """Handlers under tests/ (fixtures) must not be counted."""
        handler = NodeComplianceSweep()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            real = root / "src" / "handlers"
            real.mkdir(parents=True)
            (real / "handler_real.py").write_text("x = 1\n")
            fixture = root / "tests" / "fixtures" / "handlers"
            fixture.mkdir(parents=True)
            (fixture / "handler_fixture.py").write_text(
                'TOPIC = "onex.evt.core.x.v1"\n'
            )

            result = handler.handle(ComplianceSweepRequest(target_dirs=[tmpdir]))
            assert result.handlers_scanned == 1
            assert result.total_violations == 0


@pytest.mark.unit
class TestLogicInNodeDocstringSkip:
    """LOGIC_IN_NODE must not fire on docstring / string-literal code examples."""

    def test_docstring_class_def_not_flagged(self) -> None:
        handler = NodeComplianceSweep()
        with tempfile.TemporaryDirectory() as tmpdir:
            # node.py must live under a handlers/ dir to be discovered by the
            # scan (matching _find_handler_files discovery rules).
            node_dir = Path(tmpdir) / "src" / "nodes" / "node_y" / "handlers"
            node_dir.mkdir(parents=True)
            (node_dir / "node.py").write_text(
                '"""Example.\n\n    class MyModel(BaseModel):\n        x: int\n"""\n'
                "value = 1\n"
            )
            result = handler.handle(
                ComplianceSweepRequest(target_dirs=[tmpdir], checks=["logic-in-node"])
            )
            assert result.handlers_scanned == 1
            assert result.by_type.get("LOGIC_IN_NODE", 0) == 0

    def test_real_class_def_in_node_still_flagged(self) -> None:
        handler = NodeComplianceSweep()
        with tempfile.TemporaryDirectory() as tmpdir:
            node_dir = Path(tmpdir) / "src" / "nodes" / "node_z" / "handlers"
            node_dir.mkdir(parents=True)
            (node_dir / "node.py").write_text(
                "class RealLogic:\n    def execute(self):\n        return 1\n"
            )
            result = handler.handle(
                ComplianceSweepRequest(target_dirs=[tmpdir], checks=["logic-in-node"])
            )
            assert result.handlers_scanned == 1
            assert result.by_type.get("LOGIC_IN_NODE", 0) >= 1
