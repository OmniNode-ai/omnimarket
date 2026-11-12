"""OMN-23198 AC1: a dod-verify run removes the virtual environment it built.

h053's root filesystem filled (8.1 GB free) with 34 hermetic behaviour-check
environments (about 20 GB) that past dod-verify runs built under
``DOD_VERIFY_HERMETIC_VENV_ROOT`` and never removed. These tests run the real
handler to each end state of a run -- success, failure, timeout and an
exception out of the collector -- with a real ``uv sync`` building a real
environment, and count the environment directories left afterwards.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import cast

import pytest

from omnimarket.enums.enum_dod_verify_execution_audience import (
    EnumDodVerifyExecutionAudience,
)
from omnimarket.nodes.node_dod_verify.handlers import handler_dod_verify
from omnimarket.nodes.node_dod_verify.handlers.handler_dod_verify import (
    HandlerDodVerify,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_start_command import (
    ModelDodVerifyStartCommand,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_state import (
    EnumEvidenceCheckStatus,
    ModelEvidenceCheckResult,
)
from omnimarket.nodes.node_dod_verify.services import evidence_collector as ec_mod
from omnimarket.nodes.node_dod_verify.services.evidence_collector import (
    EvidenceCollector,
)

_UV = shutil.which("uv")

pytestmark = pytest.mark.skipif(
    _UV is None, reason="uv is not on PATH; the real-sync legs cannot run"
)


def _write_uv_project(root: Path) -> None:
    import subprocess

    (root / "pyproject.toml").write_text(
        "[project]\n"
        'name = "omn23198-fixture"\n'
        'version = "0.1.0"\n'
        'requires-python = ">=3.12"\n'
        "dependencies = []\n"
        "\n"
        "[tool.uv]\n"
        "package = false\n"
    )
    subprocess.run(
        [str(_UV), "lock"], cwd=str(root), capture_output=True, text=True, check=True
    )


class _OneUvCheckCollector(EvidenceCollector):
    """The real collector, with ``collect`` reduced to one real uv command check.

    ``_run_command_check`` is the production path that builds the hermetic
    environment; only the contract loading in front of it is replaced.
    """

    command = "uv run python -c 'pass'"
    project: Path
    boom = False

    def collect(
        self,
        ticket_id: str,
        contract_path: str | None = None,
        execution_audience: EnumDodVerifyExecutionAudience = (
            EnumDodVerifyExecutionAudience.HOSTED
        ),
    ) -> list[ModelEvidenceCheckResult]:
        ok, msg = self._run_command_check(
            {
                "check_type": "test_passes",
                "check_value": self.command,
                "cwd": str(self.project),
            },
            ticket_id,
        )
        assert self._hermetic_uv_envs, "the check must have built an environment"
        if self.boom:
            raise RuntimeError("collector blew up after building its environment")
        return [
            ModelEvidenceCheckResult(
                evidence_id="c1",
                description="one uv check",
                status=(
                    EnumEvidenceCheckStatus.VERIFIED
                    if ok
                    else EnumEvidenceCheckStatus.FAILED
                ),
                message=msg,
            )
        ]


@pytest.fixture
def hermetic_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "hermetic-envs"
    monkeypatch.setenv(ec_mod._HERMETIC_VENV_ROOT_ENV, str(root))
    monkeypatch.setenv("OMNI_HOME", str(tmp_path))
    monkeypatch.setenv("DOD_VERIFY_LIVE_PR_CHECK", "0")
    monkeypatch.delenv("DOD_VERIFY_ALLOW_STALE_PRODUCT_CLONE", raising=False)
    return root


def _count_envs(root: Path) -> int:
    return len([p for p in root.iterdir() if p.is_dir()]) if root.exists() else 0


def _run_handler(
    tmp_path: Path,
    *,
    command: str,
    boom: bool = False,
) -> ModelEvidenceCheckResult | None:
    project = tmp_path / "project"
    project.mkdir()
    _write_uv_project(project)

    class _Collector(_OneUvCheckCollector):
        pass

    _Collector.command = command
    _Collector.project = project
    _Collector.boom = boom

    class _Handler(HandlerDodVerify):
        def _make_collector(self) -> EvidenceCollector:
            return _Collector()

    state = _Handler()._handle_typed(
        ModelDodVerifyStartCommand(
            ticket_id="OMN-23198",
            execution_audience=EnumDodVerifyExecutionAudience.HOSTED,
        )
    )
    return state.checks[0] if state.checks else None


def test_environment_is_removed_when_the_run_succeeds(
    tmp_path: Path, hermetic_root: Path
) -> None:
    check = _run_handler(tmp_path, command="uv run python -c 'pass'")
    assert check is not None
    assert check.status is EnumEvidenceCheckStatus.VERIFIED
    assert _count_envs(hermetic_root) == 0


def test_environment_is_removed_when_a_check_fails(
    tmp_path: Path, hermetic_root: Path
) -> None:
    check = _run_handler(tmp_path, command="uv run python -c 'raise SystemExit(3)'")
    assert check is not None
    assert check.status is EnumEvidenceCheckStatus.FAILED
    assert _count_envs(hermetic_root) == 0


def test_environment_is_removed_when_a_check_times_out(
    tmp_path: Path, hermetic_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ec_mod._CHECK_TIMEOUT_ENV, "2")
    check = _run_handler(
        tmp_path, command="uv run python -c 'import time; time.sleep(60)'"
    )
    assert check is not None
    assert check.status is EnumEvidenceCheckStatus.FAILED
    assert _count_envs(hermetic_root) == 0


def test_environment_is_removed_when_the_collector_raises(
    tmp_path: Path, hermetic_root: Path
) -> None:
    with pytest.raises(RuntimeError, match="blew up"):
        _run_handler(tmp_path, command="uv run python -c 'pass'", boom=True)
    assert _count_envs(hermetic_root) == 0


def test_a_run_never_removes_an_environment_it_did_not_build(
    tmp_path: Path, hermetic_root: Path
) -> None:
    """A peer run's environment under the same root survives this run's close."""
    peer = hermetic_root / "peer-run-env"
    (peer / "bin").mkdir(parents=True)
    check = _run_handler(tmp_path, command="uv run python -c 'pass'")
    assert check is not None
    assert check.status is EnumEvidenceCheckStatus.VERIFIED
    assert [p.name for p in hermetic_root.iterdir()] == ["peer-run-env"]


def test_two_concurrent_runs_get_separate_environments(
    tmp_path: Path, hermetic_root: Path
) -> None:
    """Same project and lock, two collectors: separate paths, so one run's
    close can never delete the environment the other is still using."""
    project = tmp_path / "project"
    project.mkdir()
    _write_uv_project(project)
    a, b = EvidenceCollector(), EvidenceCollector()
    try:
        path_a, err_a = a._ensure_hermetic_uv_env(project.resolve())
        path_b, err_b = b._ensure_hermetic_uv_env(project.resolve())
        assert err_a is None
        assert err_b is None
        assert path_a is not None
        assert path_b is not None
        assert path_a != path_b
        a.close()
        assert not path_a.exists()
        assert (path_b / "bin" / "python").is_file()
    finally:
        a.close()
        b.close()
    assert _count_envs(hermetic_root) == 0


def test_close_is_idempotent_and_clears_the_memo(
    tmp_path: Path, hermetic_root: Path
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _write_uv_project(project)
    collector = EvidenceCollector()
    collector._ensure_hermetic_uv_env(project.resolve())
    collector.close()
    collector.close()
    assert collector._hermetic_uv_envs == {}
    assert _count_envs(hermetic_root) == 0


def test_a_stub_collector_without_close_still_runs(
    tmp_path: Path, hermetic_root: Path
) -> None:
    """``_make_collector`` is a documented seam suites replace with stubs."""
    assert hasattr(handler_dod_verify, "HandlerDodVerify")

    class _Stub:
        lookup_failure_cause = None
        lookup_failure_code = None

        def collect(self, **_: object) -> list[ModelEvidenceCheckResult]:
            return [
                ModelEvidenceCheckResult(
                    evidence_id="s",
                    description="stub",
                    status=EnumEvidenceCheckStatus.VERIFIED,
                    message="ok",
                )
            ]

    class _Handler(HandlerDodVerify):
        def _make_collector(self) -> EvidenceCollector:
            return cast(EvidenceCollector, _Stub())

    state = _Handler()._handle_typed(
        ModelDodVerifyStartCommand(
            ticket_id="OMN-23198",
            execution_audience=EnumDodVerifyExecutionAudience.HOSTED,
        )
    )
    assert state.checks
    assert state.checks[0].status is EnumEvidenceCheckStatus.VERIFIED
