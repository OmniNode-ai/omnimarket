# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-16846 D1, LOCAL path: a behaviour check builds its own environment.

The CI half of this ticket (omnibase_infra#2971, merge ``960255103``) split
the sweep's DISPATCH venv from the GATE venv and moved OMN-16759 from
``behavior_proving=0`` to ``3``. The LOCAL path was left explicitly open in
that PR body and is what this module closes.

**The collision, and why neither side is wrong.**
``scripts/reconcile-workspace-venvs.sh`` composes ``omnimarket`` into
``$OMNI_HOME/omnibase_infra/.venv`` on a <=600s tick, deliberately -- its
header calls that "layer 2", omnimarket is absent from infra's ``uv.lock`` on
purpose because the layer graph is compat -> core -> spi -> infra and
omnimarket sits ABOVE infra, and ``scripts/onex`` execs that venv's
entrypoint. Meanwhile ``omnibase_infra/tests/conftest.py`` calls
``assert_venv_purity()`` at ``pytest_configure`` and refuses any undeclared
``onex.nodes`` provider, because two providers of one node identity
manufacture DUPLICATE_REGISTRATION false REDs (OMN-15620: 25 failed / 33
passed against 58 passed clean, same tree). A ``test_passes`` check is
``uv run pytest ...`` with ``cwd: ${OMNI_HOME}/omnibase_infra``, so it
inherited that composed venv and was refused before collection.

**Measured on the operator Mac, 2026-09-06, same tree, same command** --
``uv run pytest tests/unit/gateway/test_gateway_token_minter.py -q``, the
OMN-15922 behaviour proof:

* shared canonical venv -> exit 1, ``OMN-15620 venv-purity gate: Canonical
  venv is IMPURE ... omnimarket==0.4.18``, zero tests collected;
* lock-exact ephemeral environment, identical tree -> ``19 passed in 1.32s``.

**The fix, and what was rejected.** Declaring the composition or allowlisting
the provider both leave two node providers in one interpreter -- the exact
state the 25 manufactured failures were measured in -- so both were rejected.
The check gets its OWN environment instead, built by ``uv sync --frozen``
(EXACT mode: it installs what the lock declares and removes anything else)
into a path keyed by project root + lock content, handed to uv through
``UV_PROJECT_ENVIRONMENT``. The shared venv is neither read nor written.

**The purity gate is not weakened.** It still runs -- inside the ephemeral
environment -- and passes there on the facts, because that environment
contains exactly what ``uv.lock`` declares. No override variable is set, no
name is exempted, and a build failure is recorded SKIPPED with a typed cause
(``HERMETIC_ENV_UNAVAILABLE``) rather than falling back to the shared venv.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

from omnimarket.nodes.node_dod_verify.handlers.handler_dod_verify import (
    HandlerDodVerify,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_start_command import (
    ModelDodVerifyStartCommand,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_state import (
    EnumDodVerifyStatus,
    EnumEvidenceCheckStatus,
    EnumEvidenceUnverifiableCause,
    ModelEvidenceCheckResult,
)
from omnimarket.nodes.node_dod_verify.services import evidence_collector as ec_mod
from omnimarket.nodes.node_dod_verify.services.evidence_collector import (
    EvidenceCollector,
    _hermetic_venv_path,
    _uv_project_root,
)

pytestmark = pytest.mark.unit

_UV = shutil.which("uv")
_requires_uv = pytest.mark.skipif(
    _UV is None, reason="uv is not on PATH; the real-sync legs cannot run"
)


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )
    return proc.stdout.strip()


def _make_fresh_clone(tmp_path: Path) -> Path:
    """A real, current git clone -- the collector asserts product-clone
    freshness (OMN-16846 D2) before it will run a declared-cwd check, so a
    bare directory would be refused for a reason unrelated to this test."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "--bare", "--initial-branch=dev", ".")

    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "--initial-branch=dev", ".")
    _git(seed, "config", "user.email", "t@t.invalid")
    _git(seed, "config", "user.name", "t")
    (seed / "marker.txt").write_text("v1\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-m", "first")
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "-u", "origin", "dev")

    fresh = tmp_path / "fresh"
    _git(tmp_path, "clone", str(origin), str(fresh))
    return fresh


def _write_uv_project(root: Path) -> None:
    """A real, minimal, dependency-free uv project.

    Real rather than stubbed: the whole claim is about which environment
    ``uv`` itself resolves, and a fixture that never invokes uv could not
    observe that.
    """
    (root / "pyproject.toml").write_text(
        "[project]\n"
        'name = "omn16846-fixture"\n'
        'version = "0.1.0"\n'
        'requires-python = ">=3.12"\n'
        "dependencies = []\n"
        "\n"
        "[tool.uv]\n"
        "package = false\n"
    )
    (root / "show_prefix.py").write_text("import sys\n\nprint(sys.prefix)\n")
    (root / "show_uv_env.sh").write_text(
        '#!/usr/bin/env bash\necho "${UV_PROJECT_ENVIRONMENT:-UNSET}"\n'
    )
    subprocess.run(
        [str(_UV), "lock"], cwd=str(root), capture_output=True, text=True, check=True
    )


@pytest.fixture
def collector(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> EvidenceCollector:
    # _resolve_check_cwd containment-checks every declared cwd against
    # OMNI_HOME, and these items bind to no PR, so the OMN-14207 live-state
    # lookup would shell out to `gh` for a merge state irrelevant here.
    monkeypatch.setenv("OMNI_HOME", str(tmp_path))
    monkeypatch.setenv("DOD_VERIFY_LIVE_PR_CHECK", "0")
    monkeypatch.delenv("DOD_VERIFY_ALLOW_STALE_PRODUCT_CLONE", raising=False)
    return EvidenceCollector()


@pytest.fixture
def hermetic_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "hermetic-envs"
    monkeypatch.setenv(ec_mod._HERMETIC_VENV_ROOT_ENV, str(root))
    return root


# ---------------------------------------------------------------------------
# The keying rules — a project can never borrow another project's environment
# ---------------------------------------------------------------------------


def test_project_root_requires_both_pyproject_and_lock(tmp_path: Path) -> None:
    """A ``pyproject.toml`` with no ``uv.lock`` is not a locked project.

    Without a lock there is no declared set to sync EXACTLY, so there is no
    such thing as a lock-exact environment for that directory and the routing
    must not claim one.
    """
    bare = tmp_path / "bare"
    (bare / "sub").mkdir(parents=True)
    assert _uv_project_root(bare / "sub") is None

    (bare / "pyproject.toml").write_text("[project]\n")
    assert _uv_project_root(bare / "sub") is None

    (bare / "uv.lock").write_text("version = 1\n")
    assert _uv_project_root(bare / "sub") == bare.resolve()


def test_environment_path_is_keyed_by_project_and_lock_content(
    tmp_path: Path, hermetic_root: Path
) -> None:
    a = tmp_path / "alpha"
    b = tmp_path / "beta"
    for root in (a, b):
        root.mkdir()
        (root / "pyproject.toml").write_text("[project]\n")
        (root / "uv.lock").write_text("version = 1\n")

    path_a = _hermetic_venv_path(a)
    path_b = _hermetic_venv_path(b)

    # Two projects never share one environment: a check with
    # cwd ${OMNI_HOME}/omnimarket cannot borrow omnibase_infra's.
    assert path_a != path_b
    assert path_a.parent == hermetic_root
    # Stable while the lock is unchanged (this is what makes the steady state
    # a cache-warm no-op rather than a rebuild per check) ...
    assert _hermetic_venv_path(a) == path_a
    # ... and a NEW path when the lock moves, so a lock change never mutates
    # an environment a concurrent lane is executing from.
    (a / "uv.lock").write_text("version = 1\n# moved\n")
    assert _hermetic_venv_path(a) != path_a

    # Never inside the project — the shared, composed `.venv` is the thing
    # being stepped away from.
    assert not str(path_a).startswith(str(a))


# ---------------------------------------------------------------------------
# RED before the fix: the check ran in the project's shared `.venv`
# ---------------------------------------------------------------------------


@_requires_uv
def test_a_uv_check_runs_in_a_lock_exact_environment_not_the_shared_venv(
    collector: EvidenceCollector, tmp_path: Path, hermetic_root: Path
) -> None:
    """The load-bearing assertion, executed rather than reasoned about.

    A ``.venv`` is planted in the project first, standing in for the composed
    canonical venv on the operator's machine. Before this fix the command
    resolved THAT environment — which is how a purity refusal ever reached a
    receipt. It must now resolve an environment under the hermetic root.
    """
    project = tmp_path / "project"
    project.mkdir()
    _write_uv_project(project)
    planted = project / ".venv"
    planted.mkdir()
    (planted / "SENTINEL").write_text("the shared, composed environment\n")

    ok, msg = collector._run_command_check(
        {
            "check_type": "test_passes",
            "check_value": "uv run python show_prefix.py",
            "cwd": str(project),
        },
        "OMN-16846",
    )

    assert ok, msg
    assert str(hermetic_root) in msg, msg
    assert str(planted) not in msg, msg
    # The planted environment was not adopted, populated, or repaired.
    assert sorted(p.name for p in planted.iterdir()) == ["SENTINEL"]


@_requires_uv
def test_a_non_uv_command_is_not_rerouted(
    collector: EvidenceCollector, tmp_path: Path, hermetic_root: Path
) -> None:
    """Blast radius is exactly the commands that resolve a uv environment.

    A ``gh api`` / ``grep`` / ``./verify.sh`` check has no project
    environment to redirect, so handing it one would be an unexplained
    environment difference in its verdict.
    """
    project = tmp_path / "project"
    project.mkdir()
    _write_uv_project(project)

    ok, msg = collector._run_command_check(
        {
            "check_type": "command",
            "check_value": "bash show_uv_env.sh",
            "cwd": str(project),
        },
        "OMN-16846",
    )

    assert ok, msg
    assert "UNSET" in msg, msg
    assert not hermetic_root.exists()


@_requires_uv
def test_the_environment_is_built_once_per_project_per_run(
    collector: EvidenceCollector, tmp_path: Path, hermetic_root: Path
) -> None:
    """Two checks in the same project sync once, not once each.

    Asserted on the collector's own memo rather than on wall time, which a
    loaded host would make meaningless.
    """
    project = tmp_path / "project"
    project.mkdir()
    _write_uv_project(project)

    for _ in range(2):
        ok, msg = collector._run_command_check(
            {
                "check_type": "test_passes",
                "check_value": "uv run python show_prefix.py",
                "cwd": str(project),
            },
            "OMN-16846",
        )
        assert ok, msg

    assert list(collector._hermetic_uv_envs) == [project.resolve()]


# ---------------------------------------------------------------------------
# A build failure is a typed non-result, never a fallback to the shared venv
# ---------------------------------------------------------------------------


@_requires_uv
def test_an_unbuildable_environment_is_skipped_with_a_named_cause(
    collector: EvidenceCollector,
    tmp_path: Path,
    hermetic_root: Path,
) -> None:
    """The refused fallback is the point.

    Failure is injected the way it actually happens -- a real
    ``uv sync --frozen`` against a lock uv will not accept -- rather than by
    stubbing the builder, so the arm under test is the one production takes.
    When the environment cannot be built the check does NOT quietly run in
    the shared, composed venv: that is the impure shape this ticket exists to
    stop reaching a receipt. It is recorded SKIPPED with a typed cause, which
    is strictly more blocking than the FAILED it replaces and asserts no
    defect the run never looked for.
    """
    fresh = _make_fresh_clone(tmp_path)
    (fresh / "pyproject.toml").write_text(
        "[project]\n"
        'name = "omn16846-unbuildable"\n'
        'version = "0.1.0"\n'
        'requires-python = ">=3.12"\n'
        "dependencies = []\n"
    )
    # A lock uv cannot honour under --frozen. Nothing about it is a stub: uv
    # reads it, refuses, and exits non-zero, which is the production arm.
    (fresh / "uv.lock").write_text('version = 1\nrequires-python = ">=3.12"\n')

    result = collector._check_evidence_item(
        {
            "id": "dod-behaviour",
            "description": "behaviour proof",
            "checks": [
                {
                    "check_type": "test_passes",
                    "check_value": "uv run pytest -q",
                    "cwd": str(fresh),
                }
            ],
        },
        "OMN-16846",
    )

    assert result.status is EnumEvidenceCheckStatus.SKIPPED
    assert (
        result.unverifiable_cause
        is EnumEvidenceUnverifiableCause.HERMETIC_ENV_UNAVAILABLE
    )
    assert result.message is not None
    assert "HERMETIC_ENV_UNAVAILABLE" in result.message

    # uv creates the environment directory before it reads the lock, so a
    # partial one is left at the deterministic path. A LATER run (fresh
    # process, empty memo) must not mistake that directory for a built
    # environment and run the check in it -- the sync is unconditional, so it
    # refuses again rather than inheriting a half-populated interpreter.
    assert hermetic_root.exists()
    second = EvidenceCollector()._check_evidence_item(
        {
            "id": "dod-behaviour",
            "description": "behaviour proof",
            "checks": [
                {
                    "check_type": "test_passes",
                    "check_value": "uv run pytest -q",
                    "cwd": str(fresh),
                }
            ],
        },
        "OMN-16846",
    )
    assert second.status is EnumEvidenceCheckStatus.SKIPPED
    assert (
        second.unverifiable_cause
        is EnumEvidenceUnverifiableCause.HERMETIC_ENV_UNAVAILABLE
    )


def test_the_new_cause_cannot_reach_a_verified_verdict() -> None:
    """Blocking is preserved; only the recording changed.

    A sibling check VERIFIES in the same run, so a plain (deliberate) SKIPPED
    would have let the ticket reach VERIFIED. It must not.
    """
    state = HandlerDodVerify().handle(
        ModelDodVerifyStartCommand(correlation_id=uuid4(), ticket_id="OMN-16846"),
        evidence_results=[
            ModelEvidenceCheckResult(
                evidence_id="dod-ok",
                description="a genuinely verified sibling",
                status=EnumEvidenceCheckStatus.VERIFIED,
                message="OK",
            ),
            ModelEvidenceCheckResult(
                evidence_id="dod-behaviour",
                description="the behaviour proof",
                status=EnumEvidenceCheckStatus.SKIPPED,
                unverifiable_cause=(
                    EnumEvidenceUnverifiableCause.HERMETIC_ENV_UNAVAILABLE
                ),
                message="HERMETIC_ENV_UNAVAILABLE: ...",
            ),
        ],
    )

    assert state.status is not EnumDodVerifyStatus.VERIFIED
