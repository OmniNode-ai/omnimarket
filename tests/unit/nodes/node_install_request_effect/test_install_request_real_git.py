# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The install request effect handler reads REAL git (OMN-24546).

The sibling handler test scripts a fake runner that answers by git's own words,
so it pins the handler's argv and its reading of exit codes but never git
itself. This file points the same handler at a real git repository built in
``tmp_path``: ``git init -b main`` with two commits (A, the older, then B), a
``side`` branch with a commit C that main does not hold, and the remote refs the
overlay's ``origin/`` prefix resolves created with ``git update-ref`` -- no
network, no second clone. Every git read the handler makes (``rev-parse
--verify --quiet`` for the resolution, ``merge-base --is-ancestor`` for the
merge and the older-than reads) runs through the real ``default_runner``, and so
does the declared verify, a real command: ``sys.executable -c "print(...)"``.
No sleeps, no network, and nothing read from the real home: ``GIT_CONFIG_GLOBAL``
and ``GIT_CONFIG_SYSTEM`` point at ``os.devnull`` before the first git call, so
the repository's own commits run under them too. The unit tree installer stays a
fake that echoes the commit it was asked for, so every case asserts the exact
sha real git resolved is what reached the installer and what the typed result
carries. The cases are: the branch name that resolves to B; the short sha of A
that resolves to A's full sha; the unmerged ``side`` that refuses
``ref_not_merged``; the sha nowhere in the clone that refuses
``ref_unresolved``; the older-than refusal that a ``rollback`` request gets
past; and the failing control whose verify prints a line without the expected
text, so the host rolls back to its previous verified revision.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple
from uuid import UUID

import pytest
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

from omnimarket.models.install_request import (
    EnumInstallRequestAction,
    EnumInstallRequestStatus,
    EnumInstallTarget,
    ModelInstallRequest,
    ModelInstallRequestReceived,
    ModelInstallRequestResult,
)
from omnimarket.nodes.node_install_request_effect.handlers.handler_install_request import (
    HandlerInstallRequest,
    InMemoryResultJournal,
    default_runner,
)
from omnimarket.nodes.node_install_request_effect.overlay import (
    ModelInstallHostRow,
    ModelInstallRepositoryRow,
    ModelInstallRequestEffectOverlay,
    ModelInstallUnitRow,
    ModelInstallVerifyRow,
)
from omnimarket.nodes.node_unit_tree_install_effect.models.model_unit_tree_install import (
    ModelUnitTreeInstallRequest,
    ModelUnitTreeInstallResult,
)

pytestmark = pytest.mark.unit

#: git is present on every runner this suite runs on; these tests do not skip.
GIT = shutil.which("git")
assert GIT is not None, "git is not on PATH; the real-git reads cannot be tested"

HOST = "real-git-host"
UNIT = "real-unit"
REPOSITORY = "Real-Org/real-repo"
VERIFY_NAME = "real-verify"
PUBLISHER = "real-git-lane-bot"
HOST_LOGIN = "real-git-host-login"
LANDING_BRANCH = "main"
EXPECTED_LINE = "example-unit ok"

#: The verify is a real declared command: its argv prints the expected line
#: (``GOOD``) or, for the failing control, a line without it (``BAD``).
GOOD_VERIFY_PROGRAM = f"print({EXPECTED_LINE!r})"
BAD_VERIFY_PROGRAM = "print('real-git verify failed')"

REQUESTED_AT = datetime(2026, 11, 15, 1, 0, tzinfo=UTC)
EXPIRES_AT = datetime(2026, 11, 16, 1, 0, tzinfo=UTC)
NOW = datetime(2026, 11, 15, 4, 0, tzinfo=UTC)

REQUEST_ID = UUID("8f3c2a50-6f1e-4a2b-9c3d-5e4f6a7b8c01")
ROLLBACK_REQUEST_ID = UUID("8f3c2a50-6f1e-4a2b-9c3d-5e4f6a7b8c02")
#: The id of the seeded journal result; no request under test reuses it, or the
#: decision would read it as the request's own prior answer and skip.
SEED_REQUEST_ID = UUID("8f3c2a50-6f1e-4a2b-9c3d-5e4f6a7b8c03")

_IDENTITY = (
    "-c",
    "user.name=real-git-tests",
    "-c",
    "user.email=real-git-tests@example.invalid",
)


class _RealGitRepo(NamedTuple):
    """The real clone the overlay's ``mirror_path`` names, and the shas git made."""

    path: Path
    #: The older commit on main.
    sha_a: str
    #: The commit main (and ``origin/main``) stands at.
    sha_b: str
    #: The commit only ``side`` (and ``origin/side``) holds.
    sha_c: str


def _run_git(repo: Path, *args: str) -> str:
    """One real git invocation in the tmp repository; a failure raises."""
    done = subprocess.run(
        ("git", "-C", str(repo), *args),
        capture_output=True,
        text=True,
        check=True,
        env=scrub_git_location_env(os.environ),
    )
    return done.stdout.strip()


def _commit(repo: Path, message: str, content: str) -> str:
    """One real commit on the checked-out branch: the full sha it made."""
    (repo / "tree.txt").write_text(content, encoding="utf-8")
    _run_git(repo, "add", "tree.txt")
    _run_git(repo, *_IDENTITY, "commit", "-m", message)
    return _run_git(repo, "rev-parse", "HEAD")


@pytest.fixture
def real_git_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _RealGitRepo:
    """A real git repository: main at A then B, a ``side`` with C, origin refs for both.

    The remote refs the handler reads through ``origin/`` are plain
    ``refs/remotes/origin/*`` created with ``git update-ref``, so the mirror
    read needs no second clone and no network. The git config is blinded to
    ``os.devnull`` first, so neither the developer's global config nor the
    system one can sign a commit or rewrite a ref under these tests.
    """
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    repo = tmp_path / "mirror"
    repo.mkdir()
    _run_git(repo, "init", "-b", LANDING_BRANCH)
    sha_a = _commit(repo, "a", "a\n")
    sha_b = _commit(repo, "b", "b\n")
    _run_git(repo, "checkout", "-b", "side")
    sha_c = _commit(repo, "c", "c\n")
    _run_git(repo, "checkout", LANDING_BRANCH)
    _run_git(repo, "update-ref", "refs/remotes/origin/main", sha_b)
    _run_git(repo, "update-ref", "refs/remotes/origin/side", sha_c)
    return _RealGitRepo(path=repo, sha_a=sha_a, sha_b=sha_b, sha_c=sha_c)


def _overlay(repo_path: Path, verify_program: str) -> ModelInstallRequestEffectOverlay:
    """The handler test's overlay shape, pointed at the real clone, with a real verify."""
    return ModelInstallRequestEffectOverlay(
        host_name_env="TEST_INSTALL_HOST_NAME",
        default_expiry_minutes=120,
        command_timeout_seconds=600,
        hold_retry_seconds=60,
        max_command_age_seconds=21600,
        output_max_chars=400,
        journal_dir_env="TEST_INSTALL_JOURNAL_DIR",
        journal_file="install-request-results.jsonl",
        publisher_login_env="TEST_INSTALL_PUBLISHER_LOGIN",
        owed_quiet_seconds=30,
        git_command=(GIT,),
        hosts=(
            ModelInstallHostRow(
                name=HOST,
                blocker_files=(),
                allowed_publishers=(PUBLISHER,),
                host_principal=HOST_LOGIN,
                units=(
                    ModelInstallUnitRow(
                        name=UNIT,
                        kind=EnumInstallTarget.UNIT_TREE,
                        repositories=(
                            ModelInstallRepositoryRow(
                                name=REPOSITORY,
                                mirror_path=str(repo_path),
                                remote_ref_prefix="origin/",
                                landing_branch=LANDING_BRANCH,
                                review_branches=(),
                            ),
                        ),
                        prepare_argv=(),
                        verifies=(
                            ModelInstallVerifyRow(
                                name=VERIFY_NAME,
                                argv=(sys.executable, "-c", verify_program),
                                expected_line=EXPECTED_LINE,
                            ),
                        ),
                    ),
                ),
            ),
        ),
    )


def _request(
    *,
    request_id: UUID = REQUEST_ID,
    ref: str,
    rollback: bool = False,
    requested_at: datetime = REQUESTED_AT,
    expires_at: datetime = EXPIRES_AT,
) -> ModelInstallRequest:
    """One unit-tree install request for the real clone, from the allowed lane bot."""
    return ModelInstallRequest(
        lane="real-git-lane",
        host=HOST,
        kind=EnumInstallTarget.UNIT_TREE,
        repository=REPOSITORY,
        ref=ref,
        unit=UNIT,
        verify=VERIFY_NAME,
        rollback=rollback,
        request_id=request_id,
        requested_at=requested_at,
        expires_at=expires_at,
    )


def _received(
    *, publisher: str = PUBLISHER, **overrides: object
) -> ModelInstallRequestReceived:
    """One request as the host read it off the bus, with the envelope producer."""
    return ModelInstallRequestReceived(
        request=_request(**overrides), publisher=publisher
    )


def _verified_result(*, request_id: UUID, revision: str) -> ModelInstallRequestResult:
    """One verified result as the host would have journalled it at ``revision``."""
    return ModelInstallRequestResult(
        request_id=request_id,
        host=HOST,
        unit=UNIT,
        status=EnumInstallRequestStatus.VERIFIED,
        at=NOW,
        revision=revision,
        verify_line=EXPECTED_LINE,
        reason="ok",
    )


class _EchoInstaller:
    """The unit tree installer: echoes the commit it was asked for as installed and remembers it.

    ``standing`` is the revision the host stands at: an install moves it to the commit it was asked for, and a
    ``verify`` reads it back the way the real installer reads its own record (nothing installed reads "no record").
    ``requests`` keeps the installs, ``reads`` the verifies.
    """

    def __init__(self) -> None:
        self.requests: list[ModelUnitTreeInstallRequest] = []
        self.reads: list[ModelUnitTreeInstallRequest] = []
        self.standing = ""

    def handle(
        self, request: ModelUnitTreeInstallRequest
    ) -> ModelUnitTreeInstallResult:
        if request.operation == "verify":
            self.reads.append(request)
            return ModelUnitTreeInstallResult(
                operation="verify",
                unit=request.unit,
                verdict="pass" if self.standing else "fail",
                commit=self.standing,
                detail="read" if self.standing else "no record",
            )
        self.requests.append(request)
        self.standing = request.ref
        return ModelUnitTreeInstallResult(
            operation=request.operation,
            unit=request.unit,
            verdict="pass",
            commit=request.ref,
            detail="installed",
        )


def _handler(
    repo: _RealGitRepo,
    *,
    installer: _EchoInstaller,
    journal: InMemoryResultJournal,
    verify_program: str = GOOD_VERIFY_PROGRAM,
) -> HandlerInstallRequest:
    """The real handler: real git through the real ``default_runner``, a fixed clock.

    The host stands where its journal says it last verified, unless the installer already holds a revision.
    """
    seeded = journal.latest_verified(HOST, "real-unit")
    if seeded is not None and not installer.standing:
        installer.standing = seeded.revision
    return HandlerInstallRequest(
        overlay=_overlay(repo.path, verify_program),
        executor_host=HOST,
        unit_tree_installer=installer,
        runner=default_runner,
        journal=journal,
        now=lambda: NOW,
    )


def _missing_sha(repo: _RealGitRepo) -> str:
    """A 7-character hex string no commit of the clone starts with."""
    missing = "0000000"
    while any(sha.startswith(missing) for sha in (repo.sha_a, repo.sha_b, repo.sha_c)):
        missing = f"{int(missing, 16) + 1:07x}"
    return missing


# ------------------------------------------------------------------- the goldens


def test_a_branch_name_ref_resolves_through_real_git_to_the_landing_commit(
    real_git_repo: _RealGitRepo,
) -> None:
    """The branch ref ``main`` goes through ``origin/``: git resolves it to B's
    full sha, reads B as merged, B installs and the verify's line is carried."""
    installer = _EchoInstaller()
    journal = InMemoryResultJournal()
    handled = _handler(real_git_repo, installer=installer, journal=journal).handle(
        _received(ref=LANDING_BRANCH)
    )

    assert handled.decision.action is EnumInstallRequestAction.EXECUTE
    assert handled.decision.reason == "safe"
    assert handled.result is not None
    assert handled.result.status is EnumInstallRequestStatus.VERIFIED
    assert handled.result.reason == "ok"
    assert len(real_git_repo.sha_b) == 40
    assert handled.result.revision == real_git_repo.sha_b
    assert handled.result.verify_line == EXPECTED_LINE
    assert handled.result.rolled_back is False
    assert handled.result.at == NOW
    assert handled.result.integration_line() == (
        f"installed={HOST}:{real_git_repo.sha_b} observed={EXPECTED_LINE}"
    )
    # The fake installer echoes the commit it was asked for: the resolved sha,
    # never the branch name.
    assert [r.ref for r in installer.requests] == [real_git_repo.sha_b]
    assert [r.host for r in installer.requests] == [HOST]
    assert journal.get(REQUEST_ID) is handled.result


def test_a_short_sha_ref_resolves_through_real_git_to_its_full_sha(
    real_git_repo: _RealGitRepo,
) -> None:
    """A 7-character sha of A is a sha for the handler, not a ref: real git
    resolves the abbreviation to A's full sha, which installs and verifies."""
    installer = _EchoInstaller()
    journal = InMemoryResultJournal()
    handled = _handler(real_git_repo, installer=installer, journal=journal).handle(
        _received(ref=real_git_repo.sha_a[:7])
    )

    assert handled.result is not None
    assert handled.result.status is EnumInstallRequestStatus.VERIFIED
    assert handled.result.reason == "ok"
    assert handled.result.revision == real_git_repo.sha_a
    assert handled.result.verify_line == EXPECTED_LINE
    assert [r.ref for r in installer.requests] == [real_git_repo.sha_a]
    assert journal.get(REQUEST_ID) is handled.result


# ------------------------------------------------- refusals real git answers for


def test_a_ref_main_does_not_hold_is_refused_by_real_gits_merge_read(
    real_git_repo: _RealGitRepo,
) -> None:
    """``origin/side`` resolves (real git answers the resolution), but the merge
    read finds C is no ancestor of ``origin/main``: refused, nothing installs."""
    resolution = subprocess.run(
        (
            GIT,
            "-C",
            str(real_git_repo.path),
            "rev-parse",
            "--verify",
            "--quiet",
            "origin/side^{commit}",
        ),
        capture_output=True,
        text=True,
        check=True,
    )
    assert resolution.stdout.strip() == real_git_repo.sha_c

    installer = _EchoInstaller()
    journal = InMemoryResultJournal()
    handled = _handler(real_git_repo, installer=installer, journal=journal).handle(
        _received(ref="side")
    )

    assert handled.result is not None
    assert handled.result.status is EnumInstallRequestStatus.REFUSED
    assert handled.result.reason == "ref_not_merged"
    assert handled.result.revision == ""
    assert handled.result.rolled_back is False
    assert installer.requests == []
    assert journal.get(REQUEST_ID) is handled.result


def test_a_sha_nowhere_in_the_clone_is_refused_unresolved_by_real_git(
    real_git_repo: _RealGitRepo,
) -> None:
    """A hex ref no object of the clone holds: ``rev-parse --verify --quiet``
    exits nonzero, the answer is ``ref_unresolved`` and nothing installs."""
    installer = _EchoInstaller()
    journal = InMemoryResultJournal()
    handled = _handler(real_git_repo, installer=installer, journal=journal).handle(
        _received(ref=_missing_sha(real_git_repo))
    )

    assert handled.result is not None
    assert handled.result.status is EnumInstallRequestStatus.REFUSED
    assert handled.result.reason == "ref_unresolved"
    assert handled.result.revision == ""
    assert installer.requests == []
    assert journal.get(REQUEST_ID) is handled.result


def test_an_older_commit_is_refused_and_the_rollback_request_installs_it(
    real_git_repo: _RealGitRepo,
) -> None:
    """The journal holds B as the last verified revision and real git reads A as
    its ancestor: refused ``older_than_installed``; the same request carrying
    ``rollback`` under a new id installs A and verifies."""
    journal = InMemoryResultJournal()
    journal.append(
        _verified_result(request_id=SEED_REQUEST_ID, revision=real_git_repo.sha_b)
    )
    installer = _EchoInstaller()
    handler = _handler(real_git_repo, installer=installer, journal=journal)

    refused = handler.handle(
        _received(request_id=REQUEST_ID, ref=real_git_repo.sha_a[:7])
    )
    assert refused.result is not None
    assert refused.result.status is EnumInstallRequestStatus.REFUSED
    assert refused.result.reason == "older_than_installed"
    assert real_git_repo.sha_b in refused.result.detail
    assert installer.requests == []

    # A redelivery under the refused id would skip as already answered, so the
    # deliberate go-back is a new request.
    back = handler.handle(
        _received(
            request_id=ROLLBACK_REQUEST_ID, ref=real_git_repo.sha_a, rollback=True
        )
    )
    assert back.result is not None
    assert back.result.status is EnumInstallRequestStatus.VERIFIED
    assert back.result.reason == "ok"
    assert back.result.revision == real_git_repo.sha_a
    assert [r.ref for r in installer.requests] == [real_git_repo.sha_a]


# ---------------------------------------------------------------- failing control


def test_a_verify_without_the_expected_line_fails_and_rolls_back(
    real_git_repo: _RealGitRepo,
) -> None:
    """Failing control: the verify command exits 0 but prints a line without the
    expected text, so the answer is VERIFY_FAILED and the installer is called a
    second time with the revision the journal last verified."""
    journal = InMemoryResultJournal()
    journal.append(
        _verified_result(request_id=SEED_REQUEST_ID, revision=real_git_repo.sha_a)
    )
    installer = _EchoInstaller()
    handled = _handler(
        real_git_repo,
        installer=installer,
        journal=journal,
        verify_program=BAD_VERIFY_PROGRAM,
    ).handle(_received(request_id=REQUEST_ID, ref=LANDING_BRANCH))

    assert handled.result is not None
    assert handled.result.status is EnumInstallRequestStatus.VERIFY_FAILED
    assert handled.result.reason == "verify_mismatch"
    assert handled.result.revision == real_git_repo.sha_b
    assert handled.result.rolled_back is True
    # The verify exited 0; only its line was wrong.
    assert "exit 0" in handled.result.detail
    assert f"restored {real_git_repo.sha_a}" in handled.result.detail
    assert [r.ref for r in installer.requests] == [
        real_git_repo.sha_b,
        real_git_repo.sha_a,
    ]
    assert [r.operation for r in installer.requests] == ["install", "install"]
