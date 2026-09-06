# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-17863: the hermetic behaviour-check environment, extended to JS projects.

OMN-16846 gave a Python behaviour check its own lock-exact environment so the
adjudication stopped borrowing a shared, composed venv. The same mechanism did
not exist for a JS project, and the gap has a named victim: the behaviour proof
drafted for OMN-17863 is

.. code-block:: yaml

    - id: "dod-omn17863-audit-verdict-behavior-proof"
      source: "manual"
      checks:
        - check_type: "test_passes"
          check_value: "pnpm test:audit-verdict"
          cwd: "${OMNI_HOME}/omniweb"

``test_passes`` is a semantic alias for ``command`` (OMN-16824) — the string is
run raw in ``cwd``. With no toolchain provisioning that is ``command not
found``, i.e. ``failed=1``: a verifier's own missing runtime recorded as a
product defect, which is precisely the confusion class OMN-17863 itself is
about one layer up. Running it against whatever ``node_modules`` happens to sit
in the shared clone is the other half of the same defect — that tree is
whatever the last ``pnpm install`` on the operator's machine left, not what the
lockfile declares.

So a pnpm check gets the same treatment a uv check already gets: its own
lock-exact environment, built by ``pnpm install --frozen-lockfile`` under the
version the project's own ``packageManager`` field pins, into a stage keyed by
(project root, lockfile content). The shared clone is neither installed into
nor read from, the install is charged to the hermetic build budget rather than
the per-check ceiling, and a stage that cannot be built is recorded SKIPPED
with the same typed ``HERMETIC_ENV_UNAVAILABLE`` cause rather than FAILED.

**Why a staged copy rather than a redirected ``node_modules``.** pnpm can be
told to put its modules elsewhere (``--modules-dir``), but Node — and every
bundler-based runner above it — resolves a bare import by walking *up from the
importing file's real path*, so a ``node_modules`` outside the tree is simply
not found. Redirecting it produces an environment that installs cleanly and
then cannot import anything, which would be a worse failure than the one being
fixed because it looks like a product error. The source tree is therefore
copied into the stage and the modules live inside it, which keeps the resolver
working and still leaves the canonical clone untouched.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from omnimarket.nodes.node_dod_verify.models.model_dod_verify_state import (
    EnumEvidenceCheckStatus,
    EnumEvidenceUnverifiableCause,
)
from omnimarket.nodes.node_dod_verify.services import evidence_collector as ec_mod
from omnimarket.nodes.node_dod_verify.services.evidence_collector import (
    EvidenceCollector,
    _hermetic_node_stage_path,
    _pnpm_project_root,
    _resolve_pinned_pnpm,
)

pytestmark = pytest.mark.unit

_PNPM = shutil.which("pnpm")


def _pnpm_version() -> str | None:
    if _PNPM is None:
        return None
    proc = subprocess.run(
        [_PNPM, "--version"], capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


_PNPM_VERSION = _pnpm_version()

_requires_pnpm = pytest.mark.skipif(
    _PNPM_VERSION is None,
    reason="pnpm is not resolvable on PATH; the real-install legs cannot run",
)


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )
    return proc.stdout.strip()


def _make_fresh_clone(tmp_path: Path) -> Path:
    """A real, current git clone.

    The collector asserts product-clone freshness (OMN-16846 D2) before it will
    run a declared-cwd check, so a bare directory would be refused for a reason
    unrelated to this module.
    """
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


def _write_pnpm_project(
    root: Path,
    *,
    pinned_version: str | None = None,
    lockfile: str | None = None,
) -> None:
    """A real, minimal, dependency-free pnpm project.

    Real rather than stubbed: the claim under test is about which modules tree
    and which pnpm the check resolves, and a fixture that never invokes pnpm
    could not observe either. Zero dependencies on purpose — the install must
    be network-independent so this module is not a registry availability test.
    """
    version = pinned_version or _PNPM_VERSION or "0.0.0"
    (root / "package.json").write_text(
        json.dumps(
            {
                "name": "omn17863-fixture",
                "version": "0.1.0",
                "private": True,
                "packageManager": f"pnpm@{version}",
                "scripts": {
                    # Records the tree the check actually ran in, so the
                    # assertion is on observed behaviour rather than on the
                    # collector's own account of itself. Written to a file
                    # rather than stdout because the runner truncates captured
                    # output at 200 characters, which a temp path can exceed.
                    "test:probe": (
                        "node -e \"require('fs').writeFileSync("
                        'process.env.OMN17863_PROBE_OUT, process.cwd())"'
                    ),
                },
            },
            indent=2,
        )
        + "\n"
    )
    (root / "show_cwd.sh").write_text(
        '#!/usr/bin/env bash\nprintf %s "${PWD}" > "${OMN17863_PROBE_OUT}"\n'
    )
    if lockfile is not None:
        (root / "pnpm-lock.yaml").write_text(lockfile)
        return
    proc = subprocess.run(
        [str(_PNPM), "install", "--lockfile-only"],
        cwd=str(root),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0 or not (root / "pnpm-lock.yaml").is_file():
        pytest.skip(
            "pnpm could not mint a lockfile for the fixture: "
            f"{(proc.stderr or proc.stdout).strip()[:300]}"
        )


@pytest.fixture
def collector(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> EvidenceCollector:
    # _resolve_check_cwd containment-checks every declared cwd against
    # OMNI_HOME, and these items bind to no PR, so the OMN-14207 live-state
    # lookup would otherwise shell out to `gh` for a merge state irrelevant here.
    monkeypatch.setenv("OMNI_HOME", str(tmp_path))
    monkeypatch.setenv("DOD_VERIFY_LIVE_PR_CHECK", "0")
    monkeypatch.delenv("DOD_VERIFY_ALLOW_STALE_PRODUCT_CLONE", raising=False)
    return EvidenceCollector()


@pytest.fixture
def node_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "hermetic-node"
    monkeypatch.setenv(ec_mod._HERMETIC_NODE_ROOT_ENV, str(root))
    return root


@pytest.fixture
def probe_out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Where a fixture script records the directory it actually ran in."""
    out = tmp_path / "probe-cwd.txt"
    monkeypatch.setenv("OMN17863_PROBE_OUT", str(out))
    return out


# ---------------------------------------------------------------------------
# The keying rules — a project can never borrow another project's modules
# ---------------------------------------------------------------------------


def test_pnpm_project_root_requires_both_package_json_and_lockfile(
    tmp_path: Path,
) -> None:
    """A ``package.json`` with no ``pnpm-lock.yaml`` is not a locked project.

    Without a lockfile there is no declared set to install exactly, so there is
    no such thing as a lock-exact tree for that directory and the routing must
    not claim one. Same rule ``_uv_project_root`` applies to ``uv.lock``.
    """
    lone = tmp_path / "lone"
    lone.mkdir()
    (lone / "package.json").write_text("{}\n")
    assert _pnpm_project_root(lone) is None

    locked = tmp_path / "locked"
    (locked / "nested" / "deep").mkdir(parents=True)
    (locked / "package.json").write_text("{}\n")
    (locked / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n")
    assert _pnpm_project_root(locked / "nested" / "deep") == locked.resolve()


def test_stage_path_is_keyed_by_project_and_lockfile_content(tmp_path: Path) -> None:
    """Two projects never share a stage, and a lock change mints a new one.

    The second half is what stops a lockfile edit mutating a tree a concurrent
    lane is mid-check in; the first is what stops omniweb's check resolving
    omnidash's modules.
    """
    a = tmp_path / "a"
    b = tmp_path / "b"
    for project in (a, b):
        project.mkdir()
        (project / "package.json").write_text("{}\n")
        (project / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n")

    assert _hermetic_node_stage_path(a) != _hermetic_node_stage_path(b)

    before = _hermetic_node_stage_path(a)
    (a / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\nsettings: {}\n")
    assert _hermetic_node_stage_path(a) != before


# ---------------------------------------------------------------------------
# The load-bearing behaviour — executed, not reasoned about
# ---------------------------------------------------------------------------


@_requires_pnpm
def test_a_pnpm_check_executes_in_a_lock_exact_stage_not_the_shared_clone(
    collector: EvidenceCollector, tmp_path: Path, node_root: Path, probe_out: Path
) -> None:
    """RED before this ticket in both of its arms.

    Without provisioning the command is ``command not found``; with a naive
    provisioning it runs against whatever ``node_modules`` the shared clone
    already carries. A sentinel modules tree is planted to stand in for that
    second arm — it must be neither adopted nor populated.
    """
    project = tmp_path / "project"
    project.mkdir()
    _write_pnpm_project(project)
    planted = project / "node_modules"
    planted.mkdir()
    (planted / "SENTINEL").write_text("the shared clone's own modules tree\n")

    ok, msg = collector._run_command_check(
        {
            "check_type": "test_passes",
            "check_value": "pnpm run test:probe",
            "cwd": str(project),
        },
        "OMN-17863",
    )

    assert ok, msg
    # The command executed, and it executed in the stage rather than the clone.
    assert probe_out.is_file(), f"the check never ran: {msg}"
    ran_in = Path(probe_out.read_text().strip()).resolve()
    assert ran_in != project.resolve(), msg
    assert node_root.resolve() in ran_in.parents, (ran_in, msg)
    # The canonical clone's modules tree was not adopted, populated or repaired.
    assert sorted(p.name for p in planted.iterdir()) == ["SENTINEL"]
    # The stage is a real, lock-exact tree of its own.
    stage = _hermetic_node_stage_path(project.resolve())
    assert (stage / "node_modules").is_dir()
    assert not (stage / "node_modules" / "SENTINEL").exists()


@_requires_pnpm
def test_a_non_pnpm_command_is_not_rerouted(
    collector: EvidenceCollector, tmp_path: Path, node_root: Path, probe_out: Path
) -> None:
    """Blast radius is exactly the commands that resolve a node modules tree.

    A ``gh api`` / ``grep`` / ``./verify.sh`` check has no modules tree to
    redirect, so staging it would be an unexplained working-directory
    difference in its verdict — and would silently move a relative path.
    """
    project = tmp_path / "project"
    project.mkdir()
    _write_pnpm_project(project)

    ok, msg = collector._run_command_check(
        {
            "check_type": "command",
            "check_value": "bash show_cwd.sh",
            "cwd": str(project),
        },
        "OMN-17863",
    )

    assert ok, msg
    assert probe_out.is_file(), f"the check never ran: {msg}"
    assert Path(probe_out.read_text().strip()).resolve() == project.resolve()
    assert not node_root.exists()


@_requires_pnpm
def test_the_stage_is_built_once_per_project_per_run(
    collector: EvidenceCollector, tmp_path: Path, node_root: Path, probe_out: Path
) -> None:
    """Two checks in the same project install once, not once each.

    Asserted on the collector's own memo rather than on wall time, which a
    loaded host would make meaningless.
    """
    project = tmp_path / "project"
    project.mkdir()
    _write_pnpm_project(project)

    for _ in range(2):
        ok, msg = collector._run_command_check(
            {
                "check_type": "test_passes",
                "check_value": "pnpm run test:probe",
                "cwd": str(project),
            },
            "OMN-17863",
        )
        assert ok, msg

    assert list(collector._hermetic_node_envs) == [project.resolve()]


# ---------------------------------------------------------------------------
# A build failure is a typed non-result, never a fallback to the shared clone
# ---------------------------------------------------------------------------


@_requires_pnpm
def test_an_unsatisfiable_lockfile_is_skipped_with_a_named_cause(
    collector: EvidenceCollector, tmp_path: Path, node_root: Path, probe_out: Path
) -> None:
    """The refused fallback is the point.

    Failure is injected the way it actually happens — a real
    ``pnpm install --frozen-lockfile`` against a lockfile pnpm will not accept
    — rather than by stubbing the builder, so the arm under test is the one
    production takes. The check does NOT quietly fall back to the clone's own
    modules tree, and it is not recorded FAILED: nothing looked at the product,
    so asserting a defect would be a fabricated verdict.
    """
    fresh = _make_fresh_clone(tmp_path)
    _write_pnpm_project(
        fresh,
        # A lockfile pnpm cannot honour under --frozen-lockfile: it declares a
        # dependency the manifest does not, so the two are out of sync and pnpm
        # refuses rather than resolving.
        lockfile=(
            "lockfileVersion: '9.0'\n"
            "settings:\n"
            "  autoInstallPeers: true\n"
            "  excludeLinksFromLockfile: false\n"
            "importers:\n"
            "  .:\n"
            "    dependencies:\n"
            "      omn17863-not-a-real-package:\n"
            "        specifier: ^1.0.0\n"
            "        version: 1.0.0\n"
        ),
    )

    result = collector._check_evidence_item(
        {
            "id": "dod-behaviour",
            "description": "behaviour proof",
            "checks": [
                {
                    "check_type": "test_passes",
                    "check_value": "pnpm run test:probe",
                    "cwd": str(fresh),
                }
            ],
        },
        "OMN-17863",
    )

    assert result.status is EnumEvidenceCheckStatus.SKIPPED
    assert (
        result.unverifiable_cause
        is EnumEvidenceUnverifiableCause.HERMETIC_ENV_UNAVAILABLE
    )
    assert result.message is not None
    assert "HERMETIC_ENV_UNAVAILABLE" in result.message

    # A LATER run (fresh process, empty memo) must not mistake the partial
    # stage left behind for a built one: the install is unconditional whenever
    # the modules tree is absent, so it refuses again rather than executing the
    # check against a half-populated tree.
    second = EvidenceCollector()._check_evidence_item(
        {
            "id": "dod-behaviour",
            "description": "behaviour proof",
            "checks": [
                {
                    "check_type": "test_passes",
                    "check_value": "pnpm run test:probe",
                    "cwd": str(fresh),
                }
            ],
        },
        "OMN-17863",
    )
    assert second.status is EnumEvidenceCheckStatus.SKIPPED
    assert (
        second.unverifiable_cause
        is EnumEvidenceUnverifiableCause.HERMETIC_ENV_UNAVAILABLE
    )


def test_a_host_with_no_pnpm_at_all_is_skipped_not_rejected_as_prose(
    collector: EvidenceCollector,
    tmp_path: Path,
    node_root: Path,
    probe_out: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ordering between the staging and the OMN-15382 shape guard.

    The guard's last predicate is a PATH lookup, so on a host with no pnpm
    ``pnpm run ...`` is "not a resolvable executable" and the check is FAILED
    as ``INVALID_CHECK_VALUE_NOT_A_COMMAND`` — the same
    verifier-defect-as-product-defect this ticket removes, arriving one step
    earlier than the exit 127 it was written against, and NOT caught by any
    fixture on a developer machine that happens to have pnpm.

    Found by CI rather than by design: omnimarket run 34034066290, job
    101488803404, whose runner has neither pnpm nor corepack. PATH is narrowed
    here so the case is deterministic on every host, including one that has
    pnpm.
    """
    project = tmp_path / "project"
    project.mkdir()
    _write_pnpm_project(
        project, pinned_version="11.5.3", lockfile="lockfileVersion: '9.0'\n"
    )
    tool_free_path = tmp_path / "no-tools"
    tool_free_path.mkdir()
    monkeypatch.setenv("PATH", str(tool_free_path))

    result = collector._check_evidence_item(
        {
            "id": "dod-behaviour",
            "description": "behaviour proof",
            "checks": [
                {
                    "check_type": "test_passes",
                    "check_value": "pnpm run test:probe",
                    "cwd": str(project),
                }
            ],
        },
        "OMN-17863",
    )

    assert result.status is EnumEvidenceCheckStatus.SKIPPED
    assert (
        result.unverifiable_cause
        is EnumEvidenceUnverifiableCause.HERMETIC_ENV_UNAVAILABLE
    )
    assert result.message is not None
    assert "INVALID_CHECK_VALUE_NOT_A_COMMAND" not in result.message
    # The refusal names the verifier's missing toolchain, not the product.
    assert "corepack is not on PATH" in result.message
    assert "pnpm is not on PATH" in result.message


def test_an_unhonourable_package_manager_pin_is_skipped_not_failed(
    collector: EvidenceCollector, tmp_path: Path, node_root: Path, probe_out: Path
) -> None:
    """A pin no resolvable pnpm satisfies is a toolchain non-result.

    This is the exact condition the sweep is in today: the runner has no
    corepack and no pnpm, so nothing can honour the project's pin. It must be
    named as the verifier's own missing toolchain, not reported as the product
    failing its behaviour proof.
    """
    project = tmp_path / "project"
    project.mkdir()
    _write_pnpm_project(
        project,
        pinned_version="0.0.0-omn17863-no-such-release",
        lockfile="lockfileVersion: '9.0'\n",
    )

    result = collector._check_evidence_item(
        {
            "id": "dod-behaviour",
            "description": "behaviour proof",
            "checks": [
                {
                    "check_type": "test_passes",
                    "check_value": "pnpm run test:probe",
                    "cwd": str(project),
                }
            ],
        },
        "OMN-17863",
    )

    assert result.status is EnumEvidenceCheckStatus.SKIPPED
    assert (
        result.unverifiable_cause
        is EnumEvidenceUnverifiableCause.HERMETIC_ENV_UNAVAILABLE
    )
    assert result.message is not None
    assert "0.0.0-omn17863-no-such-release" in result.message


def test_corepack_version_probe_must_also_execute_the_pinned_runner(
    tmp_path: Path,
    node_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A version echo is not enough proof that the pinned pnpm can run.

    CI found this shape first: the unavailable package-manager release reached
    the command path and was reported FAILED instead of as a toolchain
    non-result. The resolver must refuse it while it still owns the evidence,
    before any product command can run.
    """
    project = tmp_path / "project"
    project.mkdir()
    version = "0.0.0-omn17863-no-such-release"
    monkeypatch.setattr(ec_mod.shutil, "which", lambda name: f"/fake/{name}")

    def fake_run(
        argv: list[str],
        *,
        capture_output: bool,
        text: bool,
        timeout: float,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        check: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        del capture_output, text, timeout, cwd, env, check
        if argv == ["/fake/corepack", f"pnpm@{version}", "--version"]:
            return subprocess.CompletedProcess(argv, 0, stdout=version + "\n")
        if argv[:2] == ["/fake/corepack", f"pnpm@{version}"]:
            return subprocess.CompletedProcess(
                argv,
                1,
                stderr="No matching package manager release found",
            )
        if argv == ["/fake/pnpm", "--version"]:
            return subprocess.CompletedProcess(argv, 0, stdout="10.30.3\n")
        raise AssertionError(f"unexpected subprocess invocation: {argv}")

    monkeypatch.setattr(ec_mod.subprocess, "run", fake_run)

    runner, reason = _resolve_pinned_pnpm(version, project)

    assert runner is None
    assert reason is not None
    assert "reported pnpm" in reason
    assert "not executable" in reason
    assert version in reason


@_requires_pnpm
def test_the_install_is_charged_to_the_hermetic_budget_not_the_per_check_ceiling(
    collector: EvidenceCollector,
    tmp_path: Path,
    node_root: Path,
    probe_out: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC: the install has its own budget, exactly like the uv sync.

    Charging a cold pnpm store to the 30s/180s per-check ceiling would turn a
    slow download into a fake verdict — the confusion OMN-17795 had to unpick.
    Proven by making the BUILD budget the binding one: an unreachably small
    hermetic budget must produce a named build failure that cites the build
    budget's own variable, not a CHECK_BUDGET_EXCEEDED.
    """
    project = tmp_path / "project"
    project.mkdir()
    _write_pnpm_project(project)
    monkeypatch.setenv(ec_mod._HERMETIC_SYNC_TIMEOUT_ENV, "0.001")

    ok, msg = collector._run_command_check(
        {
            "check_type": "test_passes",
            "check_value": "pnpm run test:probe",
            "cwd": str(project),
        },
        "OMN-17863",
    )

    assert not ok
    assert msg.startswith(ec_mod._HERMETIC_ENV_FAILURE_MARKER), msg
    assert ec_mod._HERMETIC_SYNC_TIMEOUT_ENV in msg, msg
    assert collector._last_check_budget_exceeded is False
