"""OMN-20332: the npm and bare-Python runners execute hermetically.

An npm falsifier runs in a staged copy whose ``npm ci`` is the build step, so
the shared clone's ``node_modules`` is never rebuilt and the install is not
charged to the per-check ceiling. A bare Python falsifier does not inherit an
ambient venv or import path. A fake ``npm`` on PATH records what ran, so no
registry is touched.
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from omnimarket.nodes.node_dod_verify.services import evidence_collector as ec_mod
from omnimarket.nodes.node_dod_verify.services.evidence_collector import (
    EvidenceCollector,
)

pytestmark = pytest.mark.unit

_FAKE_NPM = """#!/usr/bin/env bash
echo "$PWD :: $*" >> "${FAKE_NPM_LOG}"
if [ "$1" = "ci" ]; then mkdir -p node_modules; fi
if [ "$1" = "ci" ]; then mkdir -p node_modules; fi
exit "${FAKE_NPM_EXIT:-0}"
"""


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("OMNI_HOME", str(tmp_path))
    monkeypatch.setenv("DOD_VERIFY_LIVE_PR_CHECK", "0")
    monkeypatch.setenv(ec_mod._HERMETIC_NODE_ROOT_ENV, str(tmp_path / "node-root"))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    npm = bin_dir / "npm"
    npm.write_text(_FAKE_NPM)
    npm.chmod(npm.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    monkeypatch.setenv("FAKE_NPM_LOG", str(tmp_path / "npm.log"))
    root = tmp_path / "omnidash"
    root.mkdir()
    (root / "package.json").write_text('{"scripts": {"test": "vitest"}}')
    (root / "package-lock.json").write_text("{}")
    return root


def _check(root: Path, value: str) -> dict[str, str]:
    return {"check_type": "test_passes", "check_value": value, "cwd": str(root)}


def test_npm_check_installs_and_runs_in_a_stage_not_the_shared_clone(
    project: Path, tmp_path: Path
) -> None:
    planted = project / "node_modules"
    planted.mkdir()
    (planted / "SENTINEL").write_text("the clone's own modules\n")

    ok, msg = EvidenceCollector()._run_command_check(
        _check(project, "npm test -- --run src/x.test.ts"), "OMN-20332"
    )

    assert ok, msg
    lines = (tmp_path / "npm.log").read_text().splitlines()
    assert [line.split(" :: ", 1)[1] for line in lines] == [
        "ci",
        "test -- --run src/x.test.ts",
    ]
    stage = Path(lines[0].split(" :: ", 1)[0])
    assert stage != project.resolve()
    assert (tmp_path / "node-root").resolve() in stage.parents
    assert {line.split(" :: ", 1)[0] for line in lines} == {str(stage)}
    # The clone's modules tree was neither rebuilt nor read.
    assert sorted(p.name for p in planted.iterdir()) == ["SENTINEL"]


def test_npm_install_runs_once_across_two_checks(project: Path, tmp_path: Path) -> None:
    collector = EvidenceCollector()
    for path in ("src/a.test.ts", "src/b.test.ts"):
        ok, msg = collector._run_command_check(
            _check(project, f"npm test -- --run {path}"), "OMN-20332"
        )
        assert ok, msg
    verbs = [
        line.split(" :: ", 1)[1].split()[0]
        for line in (tmp_path / "npm.log").read_text().splitlines()
    ]
    assert verbs == ["ci", "test", "test"]


def test_a_failing_npm_ci_is_the_verifiers_typed_failure_not_a_product_failure(
    project: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("FAKE_NPM_EXIT", "1")
    ok, msg = EvidenceCollector()._run_command_check(
        _check(project, "npm test -- --run src/x.test.ts"), "OMN-20332"
    )
    assert not ok
    assert ec_mod._HERMETIC_ENV_FAILURE_MARKER in msg
    assert "npm ci" in msg
    # The tests never ran, and the partial modules tree is not left to be
    # adopted as a finished install by the next collector.
    assert "test --" not in (tmp_path / "npm.log").read_text()
    stage = ec_mod._hermetic_node_stage_path(project, "package-lock.json")
    assert not (stage / "node_modules").exists()
    monkeypatch.setenv("FAKE_NPM_EXIT", "0")
    ok, msg = EvidenceCollector()._run_command_check(
        _check(project, "npm test -- --run src/x.test.ts"), "OMN-20332"
    )
    assert ok, msg


def test_missing_npm_is_the_typed_environment_failure(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    ok, msg = EvidenceCollector()._run_command_check(
        _check(project, "npm test -- --run src/x.test.ts"), "OMN-20332"
    )
    assert not ok
    assert ec_mod._HERMETIC_ENV_FAILURE_MARKER in msg


def test_npm_project_root_refuses_a_pnpm_project(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text("{}")
    (tmp_path / "package-lock.json").write_text("{}")
    assert ec_mod._npm_project_root(tmp_path) == tmp_path.resolve()
    (tmp_path / "pnpm-lock.yaml").write_text("")
    assert ec_mod._npm_project_root(tmp_path) is None


def test_bare_python_check_does_not_inherit_an_ambient_venv_or_import_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNI_HOME", str(tmp_path))
    monkeypatch.setenv("DOD_VERIFY_LIVE_PR_CHECK", "0")
    monkeypatch.setenv("VIRTUAL_ENV", "/ambient/venv")
    monkeypatch.setenv("PYTHONPATH", "/ambient/site-packages")
    seen: dict[str, object] = {}

    def _fake_run(argv: list[str], **kwargs: object) -> object:
        seen["env"] = kwargs.get("env")
        return type("P", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(ec_mod.subprocess, "run", _fake_run)
    repo = tmp_path / "omnidash"
    repo.mkdir()
    collector = EvidenceCollector()
    monkeypatch.setattr(ec_mod, "_invalid_check_value_reason", lambda *_a, **_k: None)
    collector._run_command_check(
        _check(repo, f"{ec_mod._BARE_PYTEST_RUNNER} tests/ci/t.py -q"), "OMN-20332"
    )
    env = seen["env"]
    assert isinstance(env, dict)
    assert "VIRTUAL_ENV" not in env
    assert "PYTHONPATH" not in env
