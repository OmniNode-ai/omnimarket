# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The install request effect handler: the golden installs and every typed answer.

The handler is built from the example overlay's values with a scripted runner
(a fake git that answers by the call's words), a fake unit tree installer, an
in-memory journal and a fixed clock, so every test reads one ``handle`` call's
decision, result and side effects. The golden cases are the unit-tree install
(decide, prepare, resolve the ref to a sha on the host's own mirror, read the
merge, install the sha, verify, answer verified) and the plugin-tree install
that reads its revision from the overlay's pattern. The refusals cover every
reason the overlay row and the host's own mirror can give before anything
installs; the failure chain covers what can fail after it, including the
rollback of a failed verify and a revision mismatch. The decision answers
(hold, ignore, skip-done, expired, publisher refusal) run nothing. The failing
controls remove one rule from a copy of the handler and show the refusal was
that rule and not some other accident.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

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
    JsonlResultJournal,
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

REQUEST_ID = UUID("5c9f6d64-1a11-4a01-9d0e-6a1b2c3d4e01")
OTHER_REQUEST_ID = UUID("5c9f6d64-1a11-4a01-9d0e-6a1b2c3d4e02")
ROLLBACK_REQUEST_ID = UUID("5c9f6d64-1a11-4a01-9d0e-6a1b2c3d4e03")
#: The id of the seeded journal result; no request under test reuses it, or the
#: decision would read it as the request's own prior answer and skip.
SEED_REQUEST_ID = UUID("5c9f6d64-1a11-4a01-9d0e-6a1b2c3d4e04")
REQUESTED_AT = datetime(2026, 11, 15, 1, 0, tzinfo=UTC)
EXPIRES_AT = datetime(2026, 11, 16, 1, 0, tzinfo=UTC)
NOW = datetime(2026, 11, 15, 4, 0, tzinfo=UTC)
OLD_REQUESTED_AT = datetime(2026, 11, 14, 1, 0, tzinfo=UTC)

#: Three commits of one history: PREVIOUS is older than NEW, OTHER is its own line.
PREVIOUS_COMMIT = "0a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d"
NEW_COMMIT = "4c3985a451847571e2cebd06a6edf1940069946e"
OTHER_COMMIT = "98f0a7b6c5d4e3f20112233445566778899aabb"

MIRROR = "/srv/example-host/mirrors/example-repo"
PLUGIN_MIRROR = "/srv/example-host/mirrors/example-plugin-repo"
ALLOWED_PUBLISHER = "example-lane-bot"


def _overlay(
    blocker: str = "/tmp/omn-19998-no-such-dir/install.lock",
) -> ModelInstallRequestEffectOverlay:
    """The example overlay's values, with a blocker path no test file sits at."""
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
        git_command=("/usr/bin/git",),
        hosts=(
            ModelInstallHostRow(
                name="example-host",
                blocker_files=(blocker,),
                allowed_publishers=(ALLOWED_PUBLISHER,),
                units=(
                    ModelInstallUnitRow(
                        name="example-unit",
                        kind=EnumInstallTarget.UNIT_TREE,
                        repositories=(
                            ModelInstallRepositoryRow(
                                name="Example-Org/example-repo",
                                mirror_path=MIRROR,
                                remote_ref_prefix="origin/",
                                landing_branch="main",
                                review_branches=("integration",),
                            ),
                        ),
                        prepare_argv=(
                            "/usr/bin/env",
                            "example-refresh",
                            "{repository}",
                            "{ref}",
                        ),
                        verifies=(
                            ModelInstallVerifyRow(
                                name="example-verify",
                                argv=(
                                    "/usr/bin/env",
                                    "example-verify",
                                    "--unit",
                                    "example-unit",
                                ),
                                expected_line="example-unit ok",
                            ),
                            ModelInstallVerifyRow(
                                name="example-verify-at",
                                argv=("/usr/bin/env", "example-verify", "--at"),
                                expected_line="example-unit ok at {revision}",
                            ),
                        ),
                    ),
                    ModelInstallUnitRow(
                        name="example-plugin-tree",
                        kind=EnumInstallTarget.PLUGIN_TREE,
                        repositories=(
                            ModelInstallRepositoryRow(
                                name="Example-Org/example-plugin-repo",
                                mirror_path=PLUGIN_MIRROR,
                                remote_ref_prefix="origin/",
                                landing_branch="main",
                            ),
                        ),
                        installer_argv=(
                            "/usr/bin/env",
                            "example-install-tree",
                            "--branch",
                            "{ref}",
                            "--commit",
                            "{sha}",
                        ),
                        rollback_argv=(
                            "/usr/bin/env",
                            "example-install-tree",
                            "--branch",
                            "{ref}",
                            "--force",
                        ),
                        verifies=(
                            ModelInstallVerifyRow(
                                name="example-verify-plugin",
                                argv=("/usr/bin/env", "example-verify-plugin"),
                                expected_line="plugin tree ok",
                            ),
                        ),
                        revision_pattern="installed revision ([0-9a-f]{7,40})",
                    ),
                ),
            ),
        ),
    )


def _request(
    *,
    request_id: UUID = REQUEST_ID,
    host: str = "example-host",
    kind: EnumInstallTarget = EnumInstallTarget.UNIT_TREE,
    repository: str = "Example-Org/example-repo",
    ref: str = "omn-19998-example",
    unit: str = "example-unit",
    verify: str = "example-verify",
    rollback: bool = False,
    requested_at: datetime = REQUESTED_AT,
    expires_at: datetime = EXPIRES_AT,
) -> ModelInstallRequest:
    """One install request on the example host, from the allowed lane bot."""
    return ModelInstallRequest(
        lane="example-lane",
        host=host,
        kind=kind,
        repository=repository,
        ref=ref,
        unit=unit,
        verify=verify,
        rollback=rollback,
        request_id=request_id,
        requested_at=requested_at,
        expires_at=expires_at,
    )


def _received(
    *, publisher: str = ALLOWED_PUBLISHER, **overrides: object
) -> ModelInstallRequestReceived:
    """One request as the host read it off the bus, with the envelope producer."""
    return ModelInstallRequestReceived(
        request=_request(**overrides),
        publisher=publisher,
    )


def _result(
    *,
    request_id: UUID = REQUEST_ID,
    unit: str = "example-unit",
    status: EnumInstallRequestStatus = EnumInstallRequestStatus.VERIFIED,
    reason: str = "ok",
    revision: str = NEW_COMMIT,
    at: datetime = NOW,
) -> ModelInstallRequestResult:
    """One result as the installer on the host would have journalled it."""
    return ModelInstallRequestResult(
        request_id=request_id,
        host="example-host",
        unit=unit,
        status=status,
        at=at,
        revision=revision,
        verify_line=(
            "example-unit ok" if status is EnumInstallRequestStatus.VERIFIED else ""
        ),
        reason=reason,
    )


class _ScriptedRunner:
    """Answers by the first key whose every word appears in the argv's words; records every argv.

    A git read, a prepare, an installer and a rollback can share one command,
    so the key is the words that tell them apart (``("merge-base",
    "origin/main")``, ``("example-install-tree", "--force")``). A call no key
    covers answers 1 with the argv in its stderr, so an unscripted call fails
    the assertions that read it.
    """

    def __init__(
        self, answers: dict[tuple[str, ...], tuple[int, str, str]] | None = None
    ) -> None:
        self.calls: list[tuple[str, ...]] = []
        self._answers = answers or {}

    def __call__(
        self, argv: Sequence[str], timeout: int
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(tuple(argv))
        spoken = " ".join(argv)
        answer = next(
            (
                value
                for key, value in self._answers.items()
                if all(word in spoken for word in key)
            ),
            None,
        )
        if answer is None:
            code, out, err = 1, "", f"unscripted: {' '.join(argv)}"
        else:
            code, out, err = answer
        return subprocess.CompletedProcess(
            args=list(argv), returncode=code, stdout=out, stderr=err
        )

    def calls_with(self, word: str) -> list[tuple[str, ...]]:
        return [call for call in self.calls if word in call]


class _FakeInstaller:
    """The unit tree installer: one verdict, one commit, every request kept."""

    def __init__(self, verdict: str = "pass", commit: str = NEW_COMMIT) -> None:
        self.requests: list[ModelUnitTreeInstallRequest] = []
        self._verdict = verdict
        self._commit = commit

    def handle(
        self, request: ModelUnitTreeInstallRequest
    ) -> ModelUnitTreeInstallResult:
        self.requests.append(request)
        return ModelUnitTreeInstallResult(
            operation=request.operation,
            unit=request.unit,
            verdict=self._verdict,
            commit=self._commit,
            detail="installed"
            if self._verdict == "pass"
            else "the tree failed its checks",
        )


def _handler(
    *,
    runner: _ScriptedRunner | None = None,
    installer: _FakeInstaller | None = None,
    journal: InMemoryResultJournal | None = None,
    overlay: ModelInstallRequestEffectOverlay | None = None,
) -> HandlerInstallRequest:
    return HandlerInstallRequest(
        overlay=overlay if overlay is not None else _overlay(),
        executor_host="example-host",
        unit_tree_installer=installer if installer is not None else _FakeInstaller(),
        runner=runner if runner is not None else _ScriptedRunner(),
        journal=journal if journal is not None else InMemoryResultJournal(),
        now=lambda: NOW,
    )


def _merged_mirror(
    *, ref: str = "omn-19998-example", sha: str = NEW_COMMIT
) -> dict[tuple[str, ...], tuple[int, str, str]]:
    """A mirror that resolves ``ref`` to ``sha`` and reads ``sha`` as merged."""
    return {
        ("example-refresh",): (0, "refreshed\n", ""),
        ("rev-parse", ref): (0, f"{sha}\n", ""),
        ("merge-base", "origin/main"): (0, "", ""),
    }


# ------------------------------------------------------------------- the goldens


def test_golden_unit_tree_install_resolves_the_branch_to_a_sha_and_installs_that() -> (
    None
):
    """Golden: prepare, resolve the branch on the host's own mirror, read the
    merge, install the resolved sha (never the branch name), verify, answer."""
    runner = _ScriptedRunner(
        answers={
            **_merged_mirror(),
            ("example-verify",): (
                0,
                "unit starting\nexample-unit ok\nunit done\n",
                "",
            ),
        }
    )
    installer = _FakeInstaller()
    journal = InMemoryResultJournal()
    handled = _handler(runner=runner, installer=installer, journal=journal).handle(
        _received()
    )

    assert handled.decision.action is EnumInstallRequestAction.EXECUTE
    assert handled.decision.reason == "safe"
    assert handled.result is not None
    assert handled.result.status is EnumInstallRequestStatus.VERIFIED
    assert handled.result.reason == "ok"
    assert handled.result.verify_line == "example-unit ok"
    assert handled.result.revision == NEW_COMMIT
    assert handled.result.rolled_back is False
    assert handled.result.host == "example-host"
    assert handled.result.unit == "example-unit"
    assert handled.result.at == NOW
    assert handled.result.integration_line() == (
        f"installed=example-host:{NEW_COMMIT} observed=example-unit ok"
    )
    assert journal.get(REQUEST_ID) == handled.result
    # The prepare command reads the request's repository and ref as today.
    assert runner.calls[0] == (
        "/usr/bin/env",
        "example-refresh",
        "Example-Org/example-repo",
        "omn-19998-example",
    )
    # The ref resolves on the host's own mirror, through the overlay's git,
    # with the repository row's remote prefix; nothing takes the message's word.
    assert runner.calls[1] == (
        "/usr/bin/git",
        "-C",
        MIRROR,
        "rev-parse",
        "--verify",
        "--quiet",
        "origin/omn-19998-example^{commit}",
    )
    # The merge is read on the mirror against the landing branch.
    assert runner.calls[2] == (
        "/usr/bin/git",
        "-C",
        MIRROR,
        "merge-base",
        "--is-ancestor",
        NEW_COMMIT,
        "origin/main",
    )
    # The verify that runs is the argv the unit's overlay declares for the name.
    assert runner.calls[3] == (
        "/usr/bin/env",
        "example-verify",
        "--unit",
        "example-unit",
    )
    assert [r.operation for r in installer.requests] == ["install"]
    assert [r.host for r in installer.requests] == ["example-host"]
    # The resolved sha is what installs, not the branch name.
    assert [r.ref for r in installer.requests] == [NEW_COMMIT]


def test_a_commit_sha_ref_reaches_the_mirror_as_it_is() -> None:
    """A ref of 7 to 40 lowercase hex characters is a sha, not a branch name."""
    runner = _ScriptedRunner(
        answers={
            **_merged_mirror(ref=NEW_COMMIT),
            ("example-verify",): (0, "example-unit ok\n", ""),
        }
    )
    installer = _FakeInstaller()
    _handler(runner=runner, installer=installer).handle(_received(ref=NEW_COMMIT))
    assert runner.calls_with("rev-parse")[0] == (
        "/usr/bin/git",
        "-C",
        MIRROR,
        "rev-parse",
        "--verify",
        "--quiet",
        f"{NEW_COMMIT}^{{commit}}",
    )
    assert [r.ref for r in installer.requests] == [NEW_COMMIT]


def test_the_verify_that_runs_is_the_one_the_request_names() -> None:
    """Two verifies are declared; the request names the second, so the second
    runs, with ``{revision}`` in its expected line reading the resolved sha."""
    runner = _ScriptedRunner(
        answers={
            **_merged_mirror(),
            ("example-verify", "--at"): (0, f"example-unit ok at {NEW_COMMIT}\n", ""),
        }
    )
    installer = _FakeInstaller()
    handled = _handler(runner=runner, installer=installer).handle(
        _received(verify="example-verify-at")
    )

    assert handled.result is not None
    assert handled.result.status is EnumInstallRequestStatus.VERIFIED
    assert handled.result.verify_line == f"example-unit ok at {NEW_COMMIT}"
    assert runner.calls_with("example-verify") == [
        ("/usr/bin/env", "example-verify", "--at")
    ]
    # The request cannot carry a command or an expected line at all.
    assert "verify_command" not in ModelInstallRequest.model_fields
    assert "expected_line" not in ModelInstallRequest.model_fields


def test_golden_plugin_tree_install_reads_the_revision_from_the_pattern() -> None:
    """Golden: the overlay's installer argv runs with the request's ref and the
    resolved sha, its pattern reads the revision back, and nothing rolls back."""
    runner = _ScriptedRunner(
        answers={
            ("rev-parse", "omn-19998-plugin"): (0, f"{NEW_COMMIT}\n", ""),
            ("merge-base", "origin/main"): (0, "", ""),
            ("example-install-tree",): (0, f"installed revision {NEW_COMMIT}\n", ""),
            ("example-verify-plugin",): (0, "plugin tree ok\n", ""),
        }
    )
    installer = _FakeInstaller()
    handled = _handler(runner=runner, installer=installer).handle(
        _received(
            kind=EnumInstallTarget.PLUGIN_TREE,
            repository="Example-Org/example-plugin-repo",
            ref="omn-19998-plugin",
            unit="example-plugin-tree",
            verify="example-verify-plugin",
        )
    )

    assert handled.result is not None
    assert handled.result.status is EnumInstallRequestStatus.VERIFIED
    assert handled.result.revision == NEW_COMMIT
    assert handled.result.rolled_back is False
    assert handled.result.verify_line == "plugin tree ok"
    assert installer.requests == []
    assert runner.calls_with("example-install-tree") == [
        (
            "/usr/bin/env",
            "example-install-tree",
            "--branch",
            "omn-19998-plugin",
            "--commit",
            NEW_COMMIT,
        )
    ]
    assert runner.calls_with("rev-parse")[0] == (
        "/usr/bin/git",
        "-C",
        PLUGIN_MIRROR,
        "rev-parse",
        "--verify",
        "--quiet",
        "origin/omn-19998-plugin^{commit}",
    )
    assert runner.calls_with("--force") == []


# ------------------------------------------- refusals before anything installs


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"unit": "no-such-unit"}, "unit_unknown"),
        ({"kind": EnumInstallTarget.PLUGIN_TREE}, "kind_mismatch"),
        ({"repository": "Other-Org/other"}, "repository_not_allowed"),
        ({"verify": "no-such-verify"}, "verify_not_declared"),
    ],
    ids=["unit", "kind", "repository", "verify"],
)
def test_a_request_the_overlay_row_does_not_cover_is_refused_before_anything_runs(
    overrides: dict[str, object], reason: str
) -> None:
    """The host's row is the boundary: an uncovered request is refused unnamed-run."""
    runner = _ScriptedRunner()
    installer = _FakeInstaller()
    journal = InMemoryResultJournal()
    handled = _handler(runner=runner, installer=installer, journal=journal).handle(
        _received(**overrides)
    )

    assert handled.decision.action is EnumInstallRequestAction.EXECUTE
    assert handled.result is not None
    assert handled.result.status is EnumInstallRequestStatus.REFUSED
    assert handled.result.reason == reason
    assert handled.result.revision == ""
    assert handled.result.verify_line == ""
    assert journal.get(REQUEST_ID) is handled.result
    assert runner.calls == []
    assert installer.requests == []


def test_a_ref_the_mirrors_own_clone_does_not_resolve_is_refused() -> None:
    """``rev-parse --verify --quiet`` nonzero: the ref resolves nowhere, so
    nothing runs and the answer names the mirror's refusal."""
    runner = _ScriptedRunner(
        answers={
            ("example-refresh",): (0, "refreshed\n", ""),
            ("rev-parse",): (1, "", "unknown revision"),
        }
    )
    installer = _FakeInstaller()
    journal = InMemoryResultJournal()
    handled = _handler(runner=runner, installer=installer, journal=journal).handle(
        _received()
    )

    assert handled.result is not None
    assert handled.result.status is EnumInstallRequestStatus.REFUSED
    assert handled.result.reason == "ref_unresolved"
    assert runner.calls_with("merge-base") == []
    assert installer.requests == []
    assert journal.get(REQUEST_ID) is handled.result


def test_a_commit_no_branch_of_the_repository_holds_is_refused() -> None:
    """The merge read exits 1 for the landing branch and every review branch:
    the sha never merged, so the installer is never called."""
    runner = _ScriptedRunner(
        answers={
            **_merged_mirror(),
            ("merge-base", "origin/main"): (1, "", ""),
            ("merge-base", "integration"): (1, "", ""),
        }
    )
    installer = _FakeInstaller()
    journal = InMemoryResultJournal()
    handled = _handler(runner=runner, installer=installer, journal=journal).handle(
        _received()
    )

    assert handled.result is not None
    assert handled.result.status is EnumInstallRequestStatus.REFUSED
    assert handled.result.reason == "ref_not_merged"
    assert handled.result.revision == ""
    # Both branches were read on the mirror; nothing else ran.
    assert sorted(call[-1] for call in runner.calls_with("merge-base")) == [
        "origin/integration",
        "origin/main",
    ]
    assert installer.requests == []
    assert runner.calls_with("example-verify") == []


def test_a_commit_only_a_review_branch_holds_is_merged() -> None:
    """A sha the landing branch does not hold but a review branch does is merged."""
    runner = _ScriptedRunner(
        answers={
            **_merged_mirror(),
            ("merge-base", "origin/main"): (1, "", ""),
            ("merge-base", "integration"): (0, "", ""),
            ("example-verify",): (0, "example-unit ok\n", ""),
        }
    )
    installer = _FakeInstaller()
    handled = _handler(runner=runner, installer=installer).handle(_received())

    assert handled.result is not None
    assert handled.result.status is EnumInstallRequestStatus.VERIFIED
    assert handled.result.reason == "ok"


def _seeded_journal(
    *, unit: str = "example-unit", revision: str = PREVIOUS_COMMIT
) -> InMemoryResultJournal:
    """A journal whose host last verified ``revision`` on ``unit``."""
    journal = InMemoryResultJournal()
    journal.append(_result(request_id=SEED_REQUEST_ID, unit=unit, revision=revision))
    return journal


def test_an_older_commit_is_refused_unless_the_request_rolls_back() -> None:
    """The journal holds a newer verified revision and the mirror reads the
    request's sha as its ancestor: refused -- unless ``rollback`` is true."""
    runner = _ScriptedRunner(
        answers={
            **_merged_mirror(ref="older", sha=PREVIOUS_COMMIT),
            ("merge-base", NEW_COMMIT): (0, "", ""),
            ("example-verify",): (0, "example-unit ok\n", ""),
        }
    )
    installer = _FakeInstaller(commit=PREVIOUS_COMMIT)
    journal = _seeded_journal(revision=NEW_COMMIT)
    handler = _handler(runner=runner, installer=installer, journal=journal)

    refused = handler.handle(_received(ref="older"))
    assert refused.result is not None
    assert refused.result.status is EnumInstallRequestStatus.REFUSED
    assert refused.result.reason == "older_than_installed"
    assert NEW_COMMIT in refused.result.detail
    assert installer.requests == []
    assert runner.calls_with("example-verify") == []

    # The sha in the refused request resolves to the older commit.
    assert runner.calls_with("rev-parse")[0] == (
        "/usr/bin/git",
        "-C",
        MIRROR,
        "rev-parse",
        "--verify",
        "--quiet",
        "origin/older^{commit}",
    )
    assert runner.calls_with("merge-base")[-1] == (
        "/usr/bin/git",
        "-C",
        MIRROR,
        "merge-base",
        "--is-ancestor",
        PREVIOUS_COMMIT,
        NEW_COMMIT,
    )

    back = handler.handle(
        _received(request_id=ROLLBACK_REQUEST_ID, ref="older", rollback=True)
    )
    assert back.result is not None
    assert back.result.status is EnumInstallRequestStatus.VERIFIED
    assert back.result.reason == "ok"
    assert back.result.revision == PREVIOUS_COMMIT
    assert [r.ref for r in installer.requests] == [PREVIOUS_COMMIT]


# ---------------------------------------------------------------- the publisher


@pytest.mark.parametrize("publisher", ["someone-else", ""], ids=["unlisted", "empty"])
def test_a_request_from_a_publisher_the_host_does_not_allow_is_refused_unjournalled(
    publisher: str,
) -> None:
    """A forged message answers refused but never touches the journal: a real
    request id keeps its idempotency."""
    runner = _ScriptedRunner()
    installer = _FakeInstaller()
    journal = InMemoryResultJournal()
    handled = _handler(runner=runner, installer=installer, journal=journal).handle(
        _received(publisher=publisher)
    )

    assert handled.decision.action is EnumInstallRequestAction.REFUSE
    assert handled.decision.reason == "publisher_not_allowed"
    assert handled.result is not None
    assert handled.result.status is EnumInstallRequestStatus.REFUSED
    assert handled.result.reason == "publisher_not_allowed"
    assert journal.get(REQUEST_ID) is None
    assert runner.calls == []
    assert installer.requests == []

    # The same id under the allowed publisher still runs: the refusal poisoned nothing.
    again = _handler(installer=installer, journal=journal).handle(_received())
    assert again.decision.action is EnumInstallRequestAction.EXECUTE


# ------------------------------------------------------- failures after install


def test_a_failing_prepare_command_fails_the_install_before_anything_installs() -> None:
    runner = _ScriptedRunner(answers={"example-refresh": (2, "", "the clone moved")})
    installer = _FakeInstaller()
    handled = _handler(runner=runner, installer=installer).handle(_received())

    assert handled.result is not None
    assert handled.result.status is EnumInstallRequestStatus.INSTALL_FAILED
    assert handled.result.reason == "prepare_failed"
    assert handled.result.detail.endswith("the clone moved")
    assert installer.requests == []


def test_an_installer_verdict_other_than_pass_fails_the_install_without_verifying() -> (
    None
):
    """A rolled-back install is answered; the verify command never runs."""
    runner = _ScriptedRunner(
        answers={
            **_merged_mirror(),
            ("example-verify",): (0, "example-unit ok\n", ""),
        }
    )
    installer = _FakeInstaller(verdict="rolled-back")
    handled = _handler(runner=runner, installer=installer).handle(_received())

    assert handled.result is not None
    assert handled.result.status is EnumInstallRequestStatus.INSTALL_FAILED
    assert handled.result.reason == "installer_rolled-back"
    assert "the tree failed its checks" in handled.result.detail
    assert runner.calls_with("example-verify") == []
    assert [r.ref for r in installer.requests] == [NEW_COMMIT]


def test_a_failing_plugin_tree_installer_fails_the_install() -> None:
    runner = _ScriptedRunner(
        answers={
            ("rev-parse", "omn-19998-plugin"): (0, f"{NEW_COMMIT}\n", ""),
            ("merge-base", "origin/main"): (0, "", ""),
            ("example-install-tree",): (3, "", "no such branch"),
            ("example-verify-plugin",): (0, "plugin tree ok\n", ""),
        }
    )
    handled = _handler(runner=runner).handle(
        _received(
            kind=EnumInstallTarget.PLUGIN_TREE,
            repository="Example-Org/example-plugin-repo",
            ref="omn-19998-plugin",
            unit="example-plugin-tree",
            verify="example-verify-plugin",
        )
    )

    assert handled.result is not None
    assert handled.result.status is EnumInstallRequestStatus.INSTALL_FAILED
    assert handled.result.reason == "installer_failed"
    assert "no such branch" in handled.result.detail
    assert runner.calls_with("example-verify-plugin") == []
    assert runner.calls_with("--force") == []


def test_an_installer_that_reports_another_revision_rolls_the_host_back() -> None:
    """The revision the installer reports must equal the checked sha: a
    different one rolls back to the previous revision and never verifies."""
    runner = _ScriptedRunner(
        answers={
            **_merged_mirror(),
            ("merge-base", PREVIOUS_COMMIT): (1, "", ""),
            ("example-verify",): (0, "example-unit ok\n", ""),
        }
    )
    installer = _FakeInstaller(commit=OTHER_COMMIT)
    journal = _seeded_journal()
    handled = _handler(runner=runner, installer=installer, journal=journal).handle(
        _received(request_id=OTHER_REQUEST_ID)
    )

    assert handled.result is not None
    assert handled.result.status is EnumInstallRequestStatus.INSTALL_FAILED
    assert handled.result.reason == "revision_mismatch"
    assert handled.result.rolled_back is True
    assert OTHER_COMMIT in handled.result.detail
    assert f"restored {PREVIOUS_COMMIT}" in handled.result.detail
    # Install the checked sha, then restore the previous revision.
    assert [r.ref for r in installer.requests] == [NEW_COMMIT, PREVIOUS_COMMIT]
    assert runner.calls_with("example-verify") == []


def test_a_failing_verify_rolls_the_unit_tree_back_to_the_previous_revision() -> None:
    """The new commit installs, the verify fails, and the installer is called a
    second time with the previous revision; the answer says it was restored."""
    runner = _ScriptedRunner(
        answers={
            **_merged_mirror(),
            ("merge-base", PREVIOUS_COMMIT): (1, "", ""),
            ("example-verify",): (1, "", "the probe is dark"),
        }
    )
    installer = _FakeInstaller()
    journal = _seeded_journal()
    handled = _handler(runner=runner, installer=installer, journal=journal).handle(
        _received(request_id=OTHER_REQUEST_ID)
    )

    assert handled.result is not None
    assert handled.result.status is EnumInstallRequestStatus.VERIFY_FAILED
    assert handled.result.reason == "verify_failed"
    assert handled.result.revision == NEW_COMMIT
    assert handled.result.rolled_back is True
    assert f"restored {PREVIOUS_COMMIT}" in handled.result.detail
    assert "the probe is dark" in handled.result.detail
    assert [r.ref for r in installer.requests] == [NEW_COMMIT, PREVIOUS_COMMIT]
    assert [r.operation for r in installer.requests] == ["install", "install"]


def test_a_failing_verify_rolls_the_plugin_tree_back_with_the_declared_argv() -> None:
    """The plugin tree's rollback is the overlay's ``rollback_argv`` with
    ``{ref}`` the previous revision, run through the runner."""
    runner = _ScriptedRunner(
        answers={
            ("rev-parse", "omn-19998-plugin"): (0, f"{NEW_COMMIT}\n", ""),
            ("merge-base", "origin/main"): (0, "", ""),
            ("merge-base", PREVIOUS_COMMIT): (1, "", ""),
            ("example-install-tree", "--force"): (0, "forced back\n", ""),
            ("example-install-tree",): (0, f"installed revision {NEW_COMMIT}\n", ""),
            ("example-verify-plugin",): (0, "nothing of note\n", ""),
        }
    )
    journal = _seeded_journal(unit="example-plugin-tree")
    handled = _handler(runner=runner, journal=journal).handle(
        _received(
            request_id=OTHER_REQUEST_ID,
            kind=EnumInstallTarget.PLUGIN_TREE,
            repository="Example-Org/example-plugin-repo",
            ref="omn-19998-plugin",
            unit="example-plugin-tree",
            verify="example-verify-plugin",
        )
    )

    assert handled.result is not None
    assert handled.result.status is EnumInstallRequestStatus.VERIFY_FAILED
    assert handled.result.reason == "verify_mismatch"
    assert handled.result.rolled_back is True
    assert f"restored {PREVIOUS_COMMIT}" in handled.result.detail
    assert runner.calls_with("--force") == [
        ("/usr/bin/env", "example-install-tree", "--branch", PREVIOUS_COMMIT, "--force")
    ]


def test_a_failing_verify_with_no_previous_revision_names_it_in_the_detail() -> None:
    """The first install of a unit has nothing to roll back to: the answer says
    so and the installer is not called a second time."""
    runner = _ScriptedRunner(
        answers={
            **_merged_mirror(),
            ("example-verify",): (4, "started\n", "the probe is dark"),
        }
    )
    installer = _FakeInstaller()
    handled = _handler(runner=runner, installer=installer).handle(_received())

    assert handled.result is not None
    assert handled.result.status is EnumInstallRequestStatus.VERIFY_FAILED
    assert handled.result.reason == "verify_failed"
    assert handled.result.rolled_back is False
    assert "no prior revision to restore" in handled.result.detail
    assert "exit 4" in handled.result.detail
    assert [r.ref for r in installer.requests] == [NEW_COMMIT]


def test_a_redelivered_request_whose_first_answer_failed_is_not_run_again() -> None:
    """A failed answer counts: the redelivery reads skip_done, the installer is
    not called a second time and the verify runs once."""
    runner = _ScriptedRunner(
        answers={
            **_merged_mirror(),
            ("example-verify",): (1, "", "the probe is dark"),
        }
    )
    installer = _FakeInstaller()
    handler = _handler(runner=runner, installer=installer)
    first = handler.handle(_received())
    again = handler.handle(_received())

    assert first.result is not None
    assert first.result.status is EnumInstallRequestStatus.VERIFY_FAILED
    assert again.decision.action is EnumInstallRequestAction.SKIP_DONE
    assert again.decision.reason == "already_answered"
    assert again.result is None
    assert len(installer.requests) == 1
    assert len(runner.calls_with("example-verify")) == 1


def test_a_new_request_for_the_commit_already_installed_runs_the_verify_only() -> None:
    """The host already verified this very commit under another request id:
    nothing installs again, the declared verify runs and the answer names it."""
    runner = _ScriptedRunner(
        answers={
            **_merged_mirror(),
            ("example-verify",): (0, "example-unit ok\n", ""),
        }
    )
    installer = _FakeInstaller()
    journal = _seeded_journal(revision=NEW_COMMIT)
    handled = _handler(runner=runner, installer=installer, journal=journal).handle(
        _received(request_id=OTHER_REQUEST_ID)
    )

    assert handled.result is not None
    assert handled.result.status is EnumInstallRequestStatus.VERIFIED
    assert handled.result.reason == "already_installed"
    assert handled.result.revision == NEW_COMMIT
    assert handled.result.verify_line == "example-unit ok"
    assert handled.result.rolled_back is False
    assert installer.requests == []
    assert len(runner.calls_with("example-verify")) == 1


def test_the_verify_only_answer_of_an_already_installed_commit_can_still_fail() -> None:
    """Nothing was reinstalled, so a failing verify on the standing commit does
    not roll back; the detail names the commit the host stands at."""
    runner = _ScriptedRunner(
        answers={
            **_merged_mirror(),
            ("example-verify",): (1, "", "the probe is dark"),
        }
    )
    installer = _FakeInstaller()
    journal = _seeded_journal(revision=NEW_COMMIT)
    handled = _handler(runner=runner, installer=installer, journal=journal).handle(
        _received(request_id=OTHER_REQUEST_ID)
    )

    assert handled.result is not None
    assert handled.result.status is EnumInstallRequestStatus.VERIFY_FAILED
    assert handled.result.reason == "verify_failed"
    assert handled.result.rolled_back is False
    assert f"already installed at {NEW_COMMIT}" in handled.result.detail
    assert installer.requests == []


# ------------------------------------------------------- the decision's answers


def test_an_expired_request_is_answered_expired_and_never_run() -> None:
    runner = _ScriptedRunner()
    installer = _FakeInstaller()
    journal = InMemoryResultJournal()
    handled = _handler(runner=runner, installer=installer, journal=journal).handle(
        _received(expires_at=datetime(2026, 11, 15, 2, 0, tzinfo=UTC))
    )

    assert handled.decision.action is EnumInstallRequestAction.EXPIRE
    assert handled.result is not None
    assert handled.result.status is EnumInstallRequestStatus.EXPIRED
    assert handled.result.reason == "expired"
    assert journal.get(REQUEST_ID) is handled.result
    assert runner.calls == []
    assert installer.requests == []


def test_a_request_older_than_the_overlay_command_age_is_expired_anyway() -> None:
    """``requested_at`` plus the overlay's 21600 seconds passed three hours ago,
    though the request's own expiry is a day ahead: expired, nothing runs."""
    runner = _ScriptedRunner()
    installer = _FakeInstaller()
    journal = InMemoryResultJournal()
    handled = _handler(runner=runner, installer=installer, journal=journal).handle(
        _received(requested_at=OLD_REQUESTED_AT)
    )

    assert handled.decision.action is EnumInstallRequestAction.EXPIRE
    assert handled.decision.reason == "expired"
    assert handled.result is not None
    assert handled.result.status is EnumInstallRequestStatus.EXPIRED
    assert journal.get(REQUEST_ID) is handled.result
    assert runner.calls == []
    assert installer.requests == []


def test_a_blocker_on_the_host_holds_the_request_and_runs_nothing(
    tmp_path: Path,
) -> None:
    blocker = tmp_path / "install.lock"
    blocker.write_text("held\n", encoding="utf-8")
    runner = _ScriptedRunner()
    installer = _FakeInstaller()
    journal = InMemoryResultJournal()
    handled = _handler(
        runner=runner,
        installer=installer,
        journal=journal,
        overlay=_overlay(blocker=str(blocker)),
    ).handle(_received())

    assert handled.decision.action is EnumInstallRequestAction.HOLD
    assert handled.decision.reason == "blocked"
    assert f"lock:{blocker}" in handled.decision.detail
    assert handled.result is None
    assert runner.calls == []
    assert installer.requests == []
    assert journal.get(REQUEST_ID) is None


def test_a_request_for_another_host_is_ignored() -> None:
    runner = _ScriptedRunner()
    installer = _FakeInstaller()
    handled = _handler(runner=runner, installer=installer).handle(
        _received(host="other-host")
    )

    assert handled.decision.action is EnumInstallRequestAction.IGNORE
    assert handled.decision.reason == "other_host"
    assert handled.result is None
    assert runner.calls == []
    assert installer.requests == []


def test_an_executor_host_the_overlay_does_not_name_is_a_deployment_error() -> None:
    handler = HandlerInstallRequest(
        overlay=_overlay(),
        executor_host="not-in-the-overlay",
        unit_tree_installer=None,
        runner=_ScriptedRunner(),
        journal=InMemoryResultJournal(),
        now=lambda: NOW,
    )
    with pytest.raises(ValueError, match="not-in-the-overlay"):
        handler.handle(_received())


def test_a_unit_tree_request_without_an_installer_is_a_deployment_error() -> None:
    runner = _ScriptedRunner(answers={**_merged_mirror()})
    handler = HandlerInstallRequest(
        overlay=_overlay(),
        executor_host="example-host",
        unit_tree_installer=None,
        runner=runner,
        journal=InMemoryResultJournal(),
        now=lambda: NOW,
    )
    with pytest.raises(ValueError, match="unit tree installer"):
        handler.handle(_received())


# ------------------------------------------------------------------- the journal


def test_the_jsonl_journal_round_trips_and_a_verified_result_wins(
    tmp_path: Path,
) -> None:
    journal = JsonlResultJournal(tmp_path / "nested" / "results.jsonl")
    assert journal.get(REQUEST_ID) is None
    assert journal.latest_verified("example-host", "example-unit") is None

    journal.append(
        _result(status=EnumInstallRequestStatus.VERIFY_FAILED, reason="verify_mismatch")
    )
    verified = _result()
    journal.append(verified)
    assert journal.get(REQUEST_ID) == verified
    assert journal.get(uuid4()) is None
    assert journal.latest_verified("example-host", "example-unit") == verified

    journal.append(
        _result(status=EnumInstallRequestStatus.VERIFY_FAILED, reason="verify_mismatch")
    )
    assert journal.get(REQUEST_ID) == verified
    # Only a verified result of that host and unit counts, the newest by ``at``.
    assert journal.latest_verified("other-host", "example-unit") is None
    assert journal.latest_verified("example-host", "other-unit") is None
    later = _result(
        request_id=OTHER_REQUEST_ID,
        revision=PREVIOUS_COMMIT,
        at=NOW + timedelta(minutes=1),
    )
    journal.append(later)
    assert journal.latest_verified("example-host", "example-unit") == later
    earliest = _result(request_id=OTHER_REQUEST_ID, at=OLD_REQUESTED_AT)
    journal.append(earliest)
    assert journal.latest_verified("example-host", "example-unit") == later


def test_the_in_memory_journal_reads_the_newest_verified_revision() -> None:
    journal = InMemoryResultJournal()
    assert journal.latest_verified("example-host", "example-unit") is None
    older = _result(request_id=OTHER_REQUEST_ID, at=OLD_REQUESTED_AT)
    newer = _result()
    journal.append(older)
    journal.append(newer)
    failed = _result(
        request_id=OTHER_REQUEST_ID, status=EnumInstallRequestStatus.REFUSED
    )
    journal.append(failed)
    assert journal.latest_verified("example-host", "example-unit") == newer
    assert journal.latest_verified("other-host", "example-unit") is None


def test_the_jsonl_journal_raises_on_an_unreadable_line(tmp_path: Path) -> None:
    """A journal that cannot be read must not silently answer ``never run``."""
    path = tmp_path / "results.jsonl"
    path.write_text("not json\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        JsonlResultJournal(path).get(REQUEST_ID)
    with pytest.raises(ValidationError):
        JsonlResultJournal(path).latest_verified("example-host", "example-unit")


def test_a_verified_result_without_a_verify_line_cannot_even_be_built() -> None:
    """Failing control: no handler path can answer verified with no observed
    line, because the result model itself refuses it."""
    with pytest.raises(ValidationError):
        ModelInstallRequestResult(
            request_id=REQUEST_ID,
            host="example-host",
            unit="example-unit",
            status=EnumInstallRequestStatus.VERIFIED,
            at=NOW,
            reason="ok",
        )


# ---------------------------------------------------------- the failing controls


class _HandlerWithoutTheMergedCheck(HandlerInstallRequest):
    """A copy of the real handler whose merged-read rule is gone."""

    def _merged(self, repository: ModelInstallRepositoryRow, sha: str) -> bool:
        return True


def test_the_merged_read_is_what_keeps_the_unmerged_commit_uninstalled() -> None:
    """Failing control: the refusal above depends on the merged read, not on
    the input. With the read removed from a copy of the handler, the same
    unmerged sha installs and verifies."""
    answers = {
        **_merged_mirror(),
        ("merge-base", "origin/main"): (1, "", ""),
        ("merge-base", "integration"): (1, "", ""),
        ("example-verify",): (0, "example-unit ok\n", ""),
    }
    real_runner = _ScriptedRunner(answers=answers)
    refused = _handler(runner=real_runner).handle(_received())
    assert refused.result is not None
    assert refused.result.status is EnumInstallRequestStatus.REFUSED
    assert refused.result.reason == "ref_not_merged"
    assert real_runner.calls_with("example-verify") == []

    loose_runner = _ScriptedRunner(answers=answers)
    loose = _HandlerWithoutTheMergedCheck(
        overlay=_overlay(),
        executor_host="example-host",
        unit_tree_installer=_FakeInstaller(),
        runner=loose_runner,
        journal=InMemoryResultJournal(),
        now=lambda: NOW,
    ).handle(_received())
    assert loose.result is not None
    assert loose.result.status is EnumInstallRequestStatus.VERIFIED
    assert len(loose_runner.calls_with("example-verify")) == 1


class _HandlerWithoutTheOlderCheck(HandlerInstallRequest):
    """A copy of the real handler whose no-going-backwards rule is gone."""

    def _is_ancestor(
        self, repository: ModelInstallRepositoryRow, sha: str, revision: str
    ) -> bool:
        # The merged read (a remote ref) still runs; only the read against the installed sha is gone.
        if revision.startswith("origin/"):
            return super()._is_ancestor(repository, sha, revision)
        return False


def test_the_older_check_is_what_keeps_the_older_commit_refused() -> None:
    """Failing control: with the ancestor read removed from a copy of the
    handler, the same older sha installs instead of refusing."""
    answers = {
        **_merged_mirror(ref="older", sha=PREVIOUS_COMMIT),
        ("merge-base", NEW_COMMIT): (0, "", ""),
        ("example-verify",): (0, "example-unit ok\n", ""),
    }
    real_runner = _ScriptedRunner(answers=answers)
    refused = _handler(
        runner=real_runner,
        installer=_FakeInstaller(commit=PREVIOUS_COMMIT),
        journal=_seeded_journal(revision=NEW_COMMIT),
    ).handle(_received(request_id=OTHER_REQUEST_ID, ref="older"))
    assert refused.result is not None
    assert refused.result.status is EnumInstallRequestStatus.REFUSED
    assert refused.result.reason == "older_than_installed"

    loose_runner = _ScriptedRunner(answers=answers)
    loose_installer = _FakeInstaller(commit=PREVIOUS_COMMIT)
    loose = _HandlerWithoutTheOlderCheck(
        overlay=_overlay(),
        executor_host="example-host",
        unit_tree_installer=loose_installer,
        runner=loose_runner,
        journal=_seeded_journal(revision=NEW_COMMIT),
        now=lambda: NOW,
    ).handle(_received(request_id=OTHER_REQUEST_ID, ref="older"))
    assert loose.result is not None
    assert loose.result.status is EnumInstallRequestStatus.VERIFIED
    assert [r.ref for r in loose_installer.requests] == [PREVIOUS_COMMIT]
