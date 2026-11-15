# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The install request effect handler: the golden installs and every typed answer.

The handler is built from the example overlay's values with a scripted runner, a
fake unit tree installer, an in-memory journal and a fixed clock, so every test
reads one ``handle`` call's decision, result and side effects. The golden cases
are the unit-tree install (decide, prepare, install, verify, answer verified)
and the plugin-tree install that reads its revision from the overlay's pattern.
The error chain covers every refusal reason before anything runs and every
failure after it, plus the decision answers (hold, ignore, skip-done, expired)
that run nothing. The last two tests are failing controls: the result model
itself refuses a verified result with no verify line, and a handler copy
without the verify prefix check does run the undeclared command, which shows
the real handler's refusal is that check and not some other accident.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from omnimarket.models.install_request import (
    EnumInstallRequestAction,
    EnumInstallRequestStatus,
    EnumInstallTarget,
    ModelInstallRequest,
    ModelInstallRequestResult,
)
from omnimarket.nodes.node_install_request_effect.handlers.handler_install_request import (
    HandlerInstallRequest,
    InMemoryResultJournal,
    JsonlResultJournal,
)
from omnimarket.nodes.node_install_request_effect.overlay import (
    ModelInstallHostRow,
    ModelInstallRequestEffectOverlay,
    ModelInstallUnitRow,
)
from omnimarket.nodes.node_unit_tree_install_effect.models.model_unit_tree_install import (
    ModelUnitTreeInstallRequest,
    ModelUnitTreeInstallResult,
)

pytestmark = pytest.mark.unit

REQUEST_ID = UUID("5c9f6d64-1a11-4a01-9d0e-6a1b2c3d4e01")
REQUESTED_AT = datetime(2026, 11, 15, 1, 0, tzinfo=UTC)
EXPIRES_AT = datetime(2026, 11, 16, 1, 0, tzinfo=UTC)
NOW = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
UNIT_TREE_COMMIT = "4c3985a451847571e2cebd06a6edf1940069946e"


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
        hosts=(
            ModelInstallHostRow(
                name="example-host",
                blocker_files=(blocker,),
                units=(
                    ModelInstallUnitRow(
                        name="example-unit",
                        kind=EnumInstallTarget.UNIT_TREE,
                        repositories=("Example-Org/example-repo",),
                        prepare_argv=(
                            "/usr/bin/env",
                            "example-refresh",
                            "{repository}",
                            "{ref}",
                        ),
                        verify_prefixes=(("/usr/bin/env", "example-verify"),),
                        branch_ref_prefix="origin/",
                    ),
                    ModelInstallUnitRow(
                        name="example-plugin-tree",
                        kind=EnumInstallTarget.PLUGIN_TREE,
                        repositories=("Example-Org/example-plugin-repo",),
                        installer_argv=(
                            "/usr/bin/env",
                            "example-install-tree",
                            "--branch",
                            "{ref}",
                        ),
                        verify_prefixes=(("/usr/bin/env", "example-verify-plugin"),),
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
    verify_command: tuple[str, ...] = (
        "/usr/bin/env",
        "example-verify",
        "--unit",
        "example-unit",
    ),
    expected_line: str = "example-unit ok",
    expires_at: datetime = EXPIRES_AT,
) -> ModelInstallRequest:
    """One install request on the example host, expiring at ``EXPIRES_AT``."""
    return ModelInstallRequest(
        lane="example-lane",
        host=host,
        kind=kind,
        repository=repository,
        ref=ref,
        unit=unit,
        verify_command=verify_command,
        expected_line=expected_line,
        request_id=request_id,
        requested_at=REQUESTED_AT,
        expires_at=expires_at,
    )


def _plugin_request() -> ModelInstallRequest:
    """The golden plugin-tree request against the overlay's plugin-tree unit."""
    return _request(
        kind=EnumInstallTarget.PLUGIN_TREE,
        repository="Example-Org/example-plugin-repo",
        ref="omn-19998-plugin",
        unit="example-plugin-tree",
        verify_command=("/usr/bin/env", "example-verify-plugin", "--tree"),
        expected_line="plugin tree ok",
    )


def _result(
    *,
    request_id: UUID = REQUEST_ID,
    status: EnumInstallRequestStatus = EnumInstallRequestStatus.VERIFIED,
    reason: str = "ok",
) -> ModelInstallRequestResult:
    """One result as the installer on the host would have journalled it."""
    return ModelInstallRequestResult(
        request_id=request_id,
        host="example-host",
        unit="example-unit",
        status=status,
        at=NOW,
        verify_line=(
            "example-unit ok" if status is EnumInstallRequestStatus.VERIFIED else ""
        ),
        reason=reason,
    )


class _ScriptedRunner:
    """Answers by the command's marker word (``argv[1]``); records every argv."""

    def __init__(self, answers: dict[str, tuple[int, str, str]] | None = None) -> None:
        self.calls: list[tuple[str, ...]] = []
        self._answers = answers or {}

    def __call__(
        self, argv: Sequence[str], timeout: int
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(tuple(argv))
        marker = argv[1] if len(argv) > 1 else argv[0]
        code, out, err = self._answers.get(marker, (0, "", ""))
        return subprocess.CompletedProcess(
            args=list(argv), returncode=code, stdout=out, stderr=err
        )

    def markers(self) -> list[str]:
        return [call[1] if len(call) > 1 else call[0] for call in self.calls]


class _FakeInstaller:
    """The unit tree installer: one verdict, one commit, every request kept."""

    def __init__(self, verdict: str = "pass", commit: str = UNIT_TREE_COMMIT) -> None:
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


def test_golden_unit_tree_install_verifies_with_the_line_and_revision() -> None:
    """Golden: prepare, install through the unit tree installer, verify, answer."""
    runner = _ScriptedRunner(
        answers={
            "example-refresh": (0, "refreshed\n", ""),
            "example-verify": (0, "unit starting\nexample-unit ok\nunit done\n", ""),
        }
    )
    installer = _FakeInstaller()
    journal = InMemoryResultJournal()
    handled = _handler(runner=runner, installer=installer, journal=journal).handle(
        _request()
    )

    assert handled.decision.action is EnumInstallRequestAction.EXECUTE
    assert handled.decision.reason == "safe"
    assert handled.result is not None
    assert handled.result.status is EnumInstallRequestStatus.VERIFIED
    assert handled.result.reason == "ok"
    assert handled.result.verify_line == "example-unit ok"
    assert handled.result.revision == UNIT_TREE_COMMIT
    assert handled.result.host == "example-host"
    assert handled.result.unit == "example-unit"
    assert handled.result.at == NOW
    assert handled.result.integration_line() == (
        f"installed=example-host:{UNIT_TREE_COMMIT} observed=example-unit ok"
    )
    assert journal.get(REQUEST_ID) == handled.result
    assert runner.markers() == ["example-refresh", "example-verify"]
    assert runner.calls[0] == (
        "/usr/bin/env",
        "example-refresh",
        "Example-Org/example-repo",
        "omn-19998-example",
    )
    assert [r.operation for r in installer.requests] == ["install"]
    assert [r.host for r in installer.requests] == ["example-host"]
    assert [r.ref for r in installer.requests] == ["origin/omn-19998-example"]


def test_a_commit_sha_ref_reaches_the_installer_as_it_is() -> None:
    """A ref of 7 to 40 lowercase hex characters is a sha, not a branch name."""
    runner = _ScriptedRunner(answers={"example-verify": (0, "example-unit ok\n", "")})
    installer = _FakeInstaller()
    _handler(runner=runner, installer=installer).handle(_request(ref=UNIT_TREE_COMMIT))
    assert [r.ref for r in installer.requests] == [UNIT_TREE_COMMIT]


def test_golden_plugin_tree_install_reads_the_revision_from_the_pattern() -> None:
    """Golden: the overlay's installer argv runs, its pattern reads the revision."""
    runner = _ScriptedRunner(
        answers={
            "example-install-tree": (0, f"installed revision {UNIT_TREE_COMMIT}\n", ""),
            "example-verify-plugin": (0, "plugin tree ok\n", ""),
        }
    )
    installer = _FakeInstaller()
    handled = _handler(runner=runner, installer=installer).handle(_plugin_request())

    assert handled.result is not None
    assert handled.result.status is EnumInstallRequestStatus.VERIFIED
    assert handled.result.revision == UNIT_TREE_COMMIT
    assert handled.result.verify_line == "plugin tree ok"
    assert installer.requests == []
    assert runner.calls[0] == (
        "/usr/bin/env",
        "example-install-tree",
        "--branch",
        "omn-19998-plugin",
    )
    assert runner.markers() == ["example-install-tree", "example-verify-plugin"]


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"unit": "no-such-unit"}, "unit_unknown"),
        ({"kind": EnumInstallTarget.PLUGIN_TREE}, "kind_mismatch"),
        ({"repository": "Other-Org/other"}, "repository_not_allowed"),
        (
            {"verify_command": ("/bin/sh", "-c", "echo pwned")},
            "verify_command_not_declared",
        ),
    ],
    ids=["unit", "kind", "repository", "verify-command"],
)
def test_a_request_the_overlay_row_does_not_cover_is_refused_before_anything_runs(
    overrides: dict[str, object], reason: str
) -> None:
    """The host's row is the boundary: an uncovered request is refused unnamed-run."""
    runner = _ScriptedRunner()
    installer = _FakeInstaller()
    journal = InMemoryResultJournal()
    handled = _handler(runner=runner, installer=installer, journal=journal).handle(
        _request(**overrides)
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


def test_an_installer_verdict_other_than_pass_fails_the_install_without_verifying() -> (
    None
):
    """A rolled-back install is answered; the verify command never runs."""
    runner = _ScriptedRunner(
        answers={
            "example-refresh": (0, "refreshed\n", ""),
            "example-verify": (0, "example-unit ok\n", ""),
        }
    )
    installer = _FakeInstaller(verdict="rolled-back")
    handled = _handler(runner=runner, installer=installer).handle(_request())

    assert handled.result is not None
    assert handled.result.status is EnumInstallRequestStatus.INSTALL_FAILED
    assert handled.result.reason == "installer_rolled-back"
    assert "the tree failed its checks" in handled.result.detail
    assert runner.markers() == ["example-refresh"]


def test_a_failing_prepare_command_fails_the_install_before_anything_installs() -> None:
    runner = _ScriptedRunner(answers={"example-refresh": (2, "", "the clone moved")})
    installer = _FakeInstaller()
    handled = _handler(runner=runner, installer=installer).handle(_request())

    assert handled.result is not None
    assert handled.result.status is EnumInstallRequestStatus.INSTALL_FAILED
    assert handled.result.reason == "prepare_failed"
    assert handled.result.detail.endswith("the clone moved")
    assert installer.requests == []


def test_a_failing_plugin_tree_installer_fails_the_install() -> None:
    runner = _ScriptedRunner(
        answers={
            "example-install-tree": (3, "", "no such branch"),
            "example-verify-plugin": (0, "plugin tree ok\n", ""),
        }
    )
    handled = _handler(runner=runner).handle(_plugin_request())

    assert handled.result is not None
    assert handled.result.status is EnumInstallRequestStatus.INSTALL_FAILED
    assert handled.result.reason == "installer_failed"
    assert "no such branch" in handled.result.detail
    assert runner.markers() == ["example-install-tree"]


def test_a_verify_command_without_the_expected_line_is_a_mismatch() -> None:
    runner = _ScriptedRunner(answers={"example-verify": (0, "nothing of note\n", "")})
    handled = _handler(runner=runner).handle(_request())

    assert handled.result is not None
    assert handled.result.status is EnumInstallRequestStatus.VERIFY_FAILED
    assert handled.result.reason == "verify_mismatch"
    assert handled.result.verify_line == ""
    assert "exit 0" in handled.result.detail
    assert "nothing of note" in handled.result.detail


def test_a_failing_verify_command_is_verify_failed_with_the_exit_code() -> None:
    runner = _ScriptedRunner(
        answers={"example-verify": (4, "started\n", "the probe is dark")}
    )
    handled = _handler(runner=runner).handle(_request())

    assert handled.result is not None
    assert handled.result.status is EnumInstallRequestStatus.VERIFY_FAILED
    assert handled.result.reason == "verify_failed"
    assert "exit 4" in handled.result.detail
    assert "the probe is dark" in handled.result.detail


def test_an_expired_request_is_answered_expired_and_never_run() -> None:
    runner = _ScriptedRunner()
    installer = _FakeInstaller()
    journal = InMemoryResultJournal()
    handled = _handler(runner=runner, installer=installer, journal=journal).handle(
        _request(expires_at=datetime(2026, 11, 15, 6, 0, tzinfo=UTC))
    )

    assert handled.decision.action is EnumInstallRequestAction.EXPIRE
    assert handled.result is not None
    assert handled.result.status is EnumInstallRequestStatus.EXPIRED
    assert handled.result.reason == "expired"
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
    ).handle(_request())

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
        _request(host="other-host")
    )

    assert handled.decision.action is EnumInstallRequestAction.IGNORE
    assert handled.decision.reason == "other_host"
    assert handled.result is None
    assert runner.calls == []
    assert installer.requests == []


def test_a_verified_request_is_not_run_twice() -> None:
    """The journal's verified result is the idempotency key: the second handle skips."""
    runner = _ScriptedRunner(
        answers={
            "example-refresh": (0, "refreshed\n", ""),
            "example-verify": (0, "example-unit ok\n", ""),
        }
    )
    installer = _FakeInstaller()
    handler = _handler(runner=runner, installer=installer)
    first = handler.handle(_request())
    again = handler.handle(_request())

    assert first.result is not None
    assert first.result.status is EnumInstallRequestStatus.VERIFIED
    assert again.decision.action is EnumInstallRequestAction.SKIP_DONE
    assert again.decision.reason == "already_verified"
    assert again.result is None
    assert len(installer.requests) == 1


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
        handler.handle(_request())


def test_a_unit_tree_request_without_an_installer_is_a_deployment_error() -> None:
    runner = _ScriptedRunner(answers={"example-refresh": (0, "", "")})
    handler = HandlerInstallRequest(
        overlay=_overlay(),
        executor_host="example-host",
        unit_tree_installer=None,
        runner=runner,
        journal=InMemoryResultJournal(),
        now=lambda: NOW,
    )
    with pytest.raises(ValueError, match="unit tree installer"):
        handler.handle(_request())


def test_the_jsonl_journal_round_trips_and_a_verified_result_wins(
    tmp_path: Path,
) -> None:
    journal = JsonlResultJournal(tmp_path / "nested" / "results.jsonl")
    assert journal.get(REQUEST_ID) is None

    journal.append(
        _result(status=EnumInstallRequestStatus.VERIFY_FAILED, reason="verify_mismatch")
    )
    verified = _result()
    journal.append(verified)
    assert journal.get(REQUEST_ID) == verified
    assert journal.get(uuid4()) is None

    journal.append(
        _result(status=EnumInstallRequestStatus.VERIFY_FAILED, reason="verify_mismatch")
    )
    assert journal.get(REQUEST_ID) == verified


def test_the_jsonl_journal_raises_on_an_unreadable_line(tmp_path: Path) -> None:
    """A journal that cannot be read must not silently answer ``never run``."""
    path = tmp_path / "results.jsonl"
    path.write_text("not json\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        JsonlResultJournal(path).get(REQUEST_ID)


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


class _HandlerWithoutThePrefixCheck(HandlerInstallRequest):
    """A copy of the real handler whose verify prefix rule is gone."""

    def _verify_command_declared(
        self, unit: ModelInstallUnitRow, verify_command: Sequence[str]
    ) -> bool:
        return True


def test_the_verify_prefix_check_is_what_keeps_the_undeclared_command_unrun() -> None:
    """Failing control: with the check removed the undeclared command runs, so
    the real handler's refusal is the check and not some other accident."""
    undeclared = ("/bin/sh", "-c", "echo pwned")

    real_runner = _ScriptedRunner()
    handled = _handler(runner=real_runner).handle(_request(verify_command=undeclared))
    assert handled.result is not None
    assert handled.result.status is EnumInstallRequestStatus.REFUSED
    assert handled.result.reason == "verify_command_not_declared"
    assert real_runner.calls == []

    loose_runner = _ScriptedRunner(
        answers={
            "-c": (0, "pwned\n", ""),
            "example-refresh": (0, "", ""),
        }
    )
    _HandlerWithoutThePrefixCheck(
        overlay=_overlay(),
        executor_host="example-host",
        unit_tree_installer=_FakeInstaller(),
        runner=loose_runner,
        journal=InMemoryResultJournal(),
        now=lambda: NOW,
    ).handle(_request(verify_command=undeclared))
    assert loose_runner.markers() == ["example-refresh", "-c"]
