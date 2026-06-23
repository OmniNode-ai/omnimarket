"""Regression tests for sweep-node dispatch-path scope resolution (OMN-13538).

Guards the systemic sweep false-clean defect: invoked the operator-canonical
no-arg way (``onex skill <name>`` -> RuntimeLocal dispatch), the sweep nodes
scanned 0 repos and returned ``status=clean / findings=[]`` because their
default-repo resolution lived only in ``__main__.py`` (which the dispatch path
never executes). A gate that silently passes is worse than no gate (Rule 5).

Covered nodes (NODE-HANDLER default-scope side):

* coverage_sweep  — empty ``target_dirs`` -> 0 repos -> clean (false-clean).
* aislop_sweep    — empty ``target_dirs`` -> loop never runs -> clean.
* gap_compute     — ``parents[5]`` fallback resolved a ``python3.12`` root on
  the deployed path (OMN-13534), scanning nothing real.
* duplication D3  — 60s subprocess cap < ~110s walltime -> TimeoutExpired ->
  quiet WARN(0) (the migration-dup check was dead inside the node).

Each sweep must, with an empty payload, resolve the canonical default repo set
and actually scan (``repos_scanned > 0``), and must FAIL LOUD (status=error /
raise) when scope is empty AND unresolvable — never report clean over zero repos.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from unittest import mock

import pytest

from omnimarket.nodes import sweep_scope
from omnimarket.nodes.sweep_scope import (
    DEFAULT_REPOS,
    SweepScopeUnresolvedError,
    require_target_dirs,
    resolve_default_target_dirs,
    resolve_omni_home,
)


def _make_synthetic_omni_home(tmp: str) -> Path:
    """Populate a synthetic ``$OMNI_HOME`` with every default repo dir."""
    omni_home = Path(tmp)
    for repo in DEFAULT_REPOS:
        (omni_home / repo).mkdir(parents=True)
    return omni_home


# ---------------------------------------------------------------------------
# Shared resolver (omnimarket.nodes.sweep_scope)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSharedScopeResolver:
    def test_default_repos_nonempty(self) -> None:
        assert len(DEFAULT_REPOS) >= 1

    def test_resolve_omni_home_prefers_explicit(self) -> None:
        assert resolve_omni_home("/explicit/home") == "/explicit/home"

    def test_resolve_omni_home_reads_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OMNI_HOME", "/env/home")
        assert resolve_omni_home() == "/env/home"

    def test_resolve_omni_home_fails_loud_when_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("OMNI_HOME", raising=False)
        with pytest.raises(SweepScopeUnresolvedError, match="OMNI_HOME is not set"):
            resolve_omni_home()

    def test_default_resolution_uses_default_repos(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            omni_home = _make_synthetic_omni_home(tmp)
            resolved = resolve_default_target_dirs([], [], omni_home)
            assert len(resolved) == len(DEFAULT_REPOS)
            for d in resolved:
                assert Path(d).is_absolute()
                assert Path(d).is_dir()

    def test_explicit_target_dirs_bypass_repo_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            resolved = resolve_default_target_dirs([tmp], [], "/nonexistent-home")
            assert resolved == [tmp]

    def test_require_target_dirs_raises_when_empty(self) -> None:
        # $OMNI_HOME exists but contains none of the default repos -> empty.
        with (
            tempfile.TemporaryDirectory() as tmp,
            pytest.raises(SweepScopeUnresolvedError, match="empty scan scope"),
        ):
            require_target_dirs([], [], tmp)

    def test_require_target_dirs_raises_without_omni_home(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("OMNI_HOME", raising=False)
        with pytest.raises(SweepScopeUnresolvedError):
            require_target_dirs([], [], None)


# ---------------------------------------------------------------------------
# coverage_sweep
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCoverageSweepDispatch:
    def _handler(self):  # type: ignore[no-untyped-def]
        from omnimarket.nodes.node_coverage_sweep.handlers.handler_coverage_sweep import (
            NodeCoverageSweep,
        )

        return NodeCoverageSweep()

    def _request(self, **kw):  # type: ignore[no-untyped-def]
        from omnimarket.nodes.node_coverage_sweep.handlers.handler_coverage_sweep import (
            CoverageSweepRequest,
        )

        return CoverageSweepRequest(**kw)

    def test_request_accepts_repos_field(self) -> None:
        req = self._request(repos=["omnibase_core"])
        assert req.repos == ["omnibase_core"]

    def test_no_arg_dispatch_scans_default_repos(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _make_synthetic_omni_home(tmp)
            monkeypatch.setenv("OMNI_HOME", tmp)
            result = self._handler().handle(self._request())
        assert result.repos_scanned == len(DEFAULT_REPOS), (
            "no-arg dispatch must resolve the default repo set, not scan zero "
            "repos and report clean"
        )

    def test_unresolvable_scope_fails_loud_not_clean(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("OMNI_HOME", raising=False)
        result = self._handler().handle(self._request())
        assert result.status == "error"
        assert result.repos_scanned == 0


# ---------------------------------------------------------------------------
# aislop_sweep
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAislopSweepDispatch:
    def _handler(self):  # type: ignore[no-untyped-def]
        from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

        from omnimarket.nodes.node_aislop_sweep.handlers.handler_aislop_sweep import (
            NodeAislopSweep,
        )

        return NodeAislopSweep(event_bus=EventBusInmemory())

    def _request(self, **kw):  # type: ignore[no-untyped-def]
        from omnimarket.nodes.node_aislop_sweep.handlers.handler_aislop_sweep import (
            AislopSweepRequest,
        )

        return AislopSweepRequest(**kw)

    def test_request_accepts_repos_field(self) -> None:
        req = self._request(repos=["omnibase_core"])
        assert req.repos == ["omnibase_core"]

    def test_no_arg_dispatch_scans_default_repos(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _make_synthetic_omni_home(tmp)
            monkeypatch.setenv("OMNI_HOME", tmp)
            result = self._handler().handle(self._request())
        assert result.repos_scanned == len(DEFAULT_REPOS), (
            "no-arg dispatch must scan the default repo set, not an empty "
            "target_dirs loop"
        )

    def test_unresolvable_scope_fails_loud_not_clean(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("OMNI_HOME", raising=False)
        result = self._handler().handle(self._request())
        assert result.status == "error"
        assert result.repos_scanned == 0


# ---------------------------------------------------------------------------
# gap_compute  (OMN-13534 — python3.12 default-scope defect)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestGapComputeDispatch:
    def _request(self, **kw):  # type: ignore[no-untyped-def]
        from omnimarket.nodes.node_gap_compute.models.model_gap_compute_request import (
            ModelGapComputeRequest,
        )

        return ModelGapComputeRequest(**kw)

    def test_default_repo_roots_resolve_omni_home_not_version_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from omnimarket.nodes.node_gap_compute.handlers.handler_gap_compute import (
            HandlerGapCompute,
        )

        with tempfile.TemporaryDirectory() as tmp:
            _make_synthetic_omni_home(tmp)
            monkeypatch.setenv("OMNI_HOME", tmp)
            roots = HandlerGapCompute()._resolve_repo_roots(self._request())
        names = {p.name for p in roots}
        assert "python3.12" not in names
        assert names == set(DEFAULT_REPOS), (
            "no-arg gap must resolve the canonical $OMNI_HOME repos, never a "
            "python3.x version token"
        )

    def test_unset_omni_home_and_non_source_layout_resolves_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Point the source-tree fallback at a dir that has no canonical repos,
        # so the python3.12-style version-token root is never adopted.
        from omnimarket.nodes.node_gap_compute.handlers import handler_gap_compute

        monkeypatch.delenv("OMNI_HOME", raising=False)
        with tempfile.TemporaryDirectory() as tmp:
            monkeypatch.setattr(handler_gap_compute, "_OMNI_HOME", Path(tmp))
            roots = handler_gap_compute.HandlerGapCompute()._resolve_repo_roots(
                self._request()
            )
        assert roots == []

    def test_no_arg_detect_with_synthetic_home_is_not_blocked_by_bad_root(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from omnimarket.nodes.node_gap_compute.handlers.handler_gap_compute import (
            HandlerGapCompute,
        )

        with tempfile.TemporaryDirectory() as tmp:
            omni_home = _make_synthetic_omni_home(tmp)
            # give one repo a contract so the detect path has something real
            contract = omni_home / DEFAULT_REPOS[0] / "node_x" / "contract.yaml"
            contract.parent.mkdir(parents=True)
            contract.write_text("name: x\nnode_type: compute\n")
            monkeypatch.setenv("OMNI_HOME", tmp)
            result = HandlerGapCompute().handle(self._request())
        assert set(result.repos_in_scope) == set(DEFAULT_REPOS)


# ---------------------------------------------------------------------------
# duplication_sweep  (D3 timeout + unresolvable scope)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDuplicationSweepDispatch:
    def _request(self, **kw):  # type: ignore[no-untyped-def]
        from omnimarket.nodes.node_duplication_sweep.handlers.handler_duplication_sweep import (
            DuplicationSweepRequest,
        )

        return DuplicationSweepRequest(**kw)

    def test_unresolvable_scope_fails_loud_not_pass(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from omnimarket.nodes.node_duplication_sweep.handlers.handler_duplication_sweep import (
            NodeDuplicationSweep,
        )

        monkeypatch.delenv("OMNI_HOME", raising=False)
        result = NodeDuplicationSweep().handle(self._request(omni_home=""))
        assert result.overall_status == "ERROR", (
            "an empty/unresolvable omni_home must not report PASS over a "
            "non-existent scan root"
        )

    def test_d3_timeout_is_fail_not_quiet_warn(self) -> None:
        from omnimarket.nodes.node_duplication_sweep.handlers import (
            handler_duplication_sweep as h,
        )

        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(
                h, "_migration_conflict_command", return_value=(["true"], Path(tmp))
            ),
            mock.patch.object(
                h.subprocess,
                "run",
                side_effect=subprocess.TimeoutExpired(cmd="x", timeout=240),
            ),
        ):
            res = h._check_d3_migration_prefixes(tmp)
        assert res.status == "FAIL", (
            "a real D3 subprocess timeout must FAIL the check, not degrade to a "
            "quiet WARN(0) (the migration-dup check must not be silently dead)"
        )

    def test_d3_timeout_budget_raised(self) -> None:
        from omnimarket.nodes.node_duplication_sweep.handlers import (
            handler_duplication_sweep as h,
        )

        assert h._D3_SUBPROCESS_TIMEOUT_S >= 180, (
            "D3 subprocess budget must exceed the real ~110s walltime"
        )


@pytest.mark.unit
def test_sweep_scope_module_exports_are_stable() -> None:
    """Lock the shared resolver's public surface (imported by 4 nodes)."""
    for name in (
        "DEFAULT_REPOS",
        "SweepScopeUnresolvedError",
        "require_target_dirs",
        "resolve_default_target_dirs",
        "resolve_omni_home",
        "resolve_repo_dirs",
    ):
        assert hasattr(sweep_scope, name)
