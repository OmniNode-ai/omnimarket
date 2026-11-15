# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Execute one install request on the host this handler serves (OMN-19998, root 5).

The effect half of the install request: read this host's own overlay row (the
deployment names what may be installed, a request cannot widen it), decide with
node_install_request_decision_compute, and only then touch the host. A request
for another host, one already answered under its own id and one whose host is
not safe yet is answered by its decision alone; a request from a login the host
does not allow, or one that names no publisher, is answered ``refused`` and
never journalled, so a forged message cannot poison the idempotency of a real
request id; an expired one is answered ``expired`` without running anything.

What does run is the host's own declared shape only: the declared prepare
command (the mirror refresh), the host's own git reading its own mirror clone
(the ref resolves to one commit sha there, which is the only thing installed,
and that sha must already be merged into the repository's landing branch or one
of its review branches), the declared installer (node_unit_tree_install_effect
for a unit tree, the overlay's installer argv for the plugin tree) and the
verify the request NAMES by name -- the verify's argv and expected line live in
the overlay, never in the message, so nothing a request carries is ever run.
The revision the installer reports must equal the resolved sha. A verify that
fails, or a revision that differs, rolls the host back to its previous verified
revision before the answer is written, and the answer says whether it did.

Every setting is an overlay value: no timeout, path, prefix, git command or
host name lives in this file. The only literals are the sha pattern that
decides whether a ref is a commit sha or a branch name, and git's own fixed
subcommand words.

Every result is appended to the journal (the request id is the idempotency key)
and returned for publication -- except a publisher refusal, which is answered
but never journalled.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Protocol
from uuid import UUID

from omnimarket.models.install_request import (
    EnumInstallRequestAction,
    EnumInstallRequestStatus,
    EnumInstallTarget,
    ModelInstallRequest,
    ModelInstallRequestDecision,
    ModelInstallRequestDecisionInput,
    ModelInstallRequestReceived,
    ModelInstallRequestResult,
)
from omnimarket.nodes.node_install_request_decision_compute.handlers.handler_install_request_decision import (
    HandlerInstallRequestDecision,
)
from omnimarket.nodes.node_install_request_effect.models.model_install_request_effect import (
    ModelInstallRequestHandled,
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

#: A ref that is entirely 7 to 40 lowercase hex characters is a commit sha and
#: reaches the mirror as it is; anything else is read through the repository
#: row's own remote ref prefix.
_SHA = re.compile(r"[0-9a-f]{7,40}")

Runner = Callable[[Sequence[str], int], subprocess.CompletedProcess[str]]


def default_runner(
    argv: Sequence[str], timeout: int
) -> subprocess.CompletedProcess[str]:
    """Run ``argv`` with no shell; a missing command reads 127 and a timeout 124."""
    try:
        return subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except FileNotFoundError:
        return subprocess.CompletedProcess(
            args=list(argv), returncode=127, stdout="", stderr="command not found"
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            args=list(argv), returncode=124, stdout="", stderr="timed out"
        )


class ProtocolUnitTreeInstaller(Protocol):
    """The installer a unit-tree request is delegated to (node_unit_tree_install_effect)."""

    def handle(
        self, request: ModelUnitTreeInstallRequest
    ) -> ModelUnitTreeInstallResult: ...


class ProtocolResultJournal(Protocol):
    """The results already published: the last per request id, and the last installed."""

    def get(self, request_id: UUID) -> ModelInstallRequestResult | None: ...

    def latest_verified(
        self, host: str, unit: str
    ) -> ModelInstallRequestResult | None: ...

    def append(self, result: ModelInstallRequestResult) -> None: ...


def _verified_of(
    results: Iterable[ModelInstallRequestResult], host: str, unit: str
) -> ModelInstallRequestResult | None:
    """The verified result of one host and unit with the greatest ``at``; None when there is none."""
    return max(
        (
            result
            for result in results
            if result.status is EnumInstallRequestStatus.VERIFIED
            and result.host == host
            and result.unit == unit
        ),
        key=lambda result: result.at,
        default=None,
    )


class InMemoryResultJournal(dict[UUID, ModelInstallRequestResult]):
    """The journal of one process: the last result per request id."""

    def append(self, result: ModelInstallRequestResult) -> None:
        self[result.request_id] = result

    def latest_verified(self, host: str, unit: str) -> ModelInstallRequestResult | None:
        return _verified_of(self.values(), host, unit)


class JsonlResultJournal:
    """One JSON line per result, appended to the file; a missing file reads empty.

    ``get`` returns the last line the id owns, except that a verified result
    wins over any other line for the same id: once a request is verified it
    stays verified, whatever a later re-run wrote. ``latest_verified`` returns
    the verified result of one host and unit with the greatest ``at``, the
    revision the host last stood at. An unreadable line raises -- the journal
    is the idempotency key, and a swallowed read error would silently re-run an
    install.
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    def append(self, result: ModelInstallRequestResult) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as stream:
            stream.write(result.model_dump_json() + "\n")

    def _lines(self) -> list[ModelInstallRequestResult]:
        if not self._path.is_file():
            return []
        return [
            ModelInstallRequestResult.model_validate_json(line)
            for line in self._path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def get(self, request_id: UUID) -> ModelInstallRequestResult | None:
        last: ModelInstallRequestResult | None = None
        verified: ModelInstallRequestResult | None = None
        for result in self._lines():
            if result.request_id != request_id:
                continue
            last = result
            if result.status is EnumInstallRequestStatus.VERIFIED:
                verified = result
        return verified if verified is not None else last

    def latest_verified(self, host: str, unit: str) -> ModelInstallRequestResult | None:
        return _verified_of(self._lines(), host, unit)


class HandlerInstallRequest:
    """``handle(received) -> ModelInstallRequestHandled``: decide, resolve, install, verify, answer."""

    def __init__(
        self,
        overlay: ModelInstallRequestEffectOverlay,
        executor_host: str,
        unit_tree_installer: ProtocolUnitTreeInstaller | None,
        runner: Runner,
        journal: ProtocolResultJournal,
        now: Callable[[], datetime],
        decision: HandlerInstallRequestDecision | None = None,
    ) -> None:
        self.overlay = overlay
        self.executor_host = executor_host
        self.unit_tree_installer = unit_tree_installer
        self.runner = runner
        self.journal = journal
        self.now = now
        self._decision = (
            decision if decision is not None else HandlerInstallRequestDecision()
        )

    def handle(
        self, received: ModelInstallRequestReceived
    ) -> ModelInstallRequestHandled:
        request = received.request
        row = self.overlay.host(self.executor_host)
        if row is None:
            raise ValueError(
                f"{self.executor_host} is no host in the install overlay;"
                " the deployment named a host this effect cannot serve"
            )
        blockers = tuple(
            f"lock:{path}" for path in row.blocker_files if Path(path).exists()
        )
        decision = self._decide(received, row, blockers)
        if decision.action in (
            EnumInstallRequestAction.IGNORE,
            EnumInstallRequestAction.SKIP_DONE,
            EnumInstallRequestAction.HOLD,
        ):
            return ModelInstallRequestHandled(decision=decision, result=None)
        if decision.action is EnumInstallRequestAction.REFUSE:
            # Answered but never journalled: a forged message must not poison
            # the idempotency of a real request id.
            return self._answer(
                decision,
                request,
                status=EnumInstallRequestStatus.REFUSED,
                reason=decision.reason,
                journal=False,
            )
        if decision.action is EnumInstallRequestAction.EXPIRE:
            return self._answer(
                decision,
                request,
                status=EnumInstallRequestStatus.EXPIRED,
                reason="expired",
            )
        return self._execute(decision, received, row)

    # ------------------------------------------------------------------ execute

    def _execute(
        self,
        decision: ModelInstallRequestDecision,
        received: ModelInstallRequestReceived,
        row: ModelInstallHostRow,
    ) -> ModelInstallRequestHandled:
        """Refuse what the host's overlay row does not cover, then install and verify."""
        request = received.request
        unit = next((u for u in row.units if u.name == request.unit), None)
        if unit is None:
            return self._answer(
                decision,
                request,
                status=EnumInstallRequestStatus.REFUSED,
                reason="unit_unknown",
            )
        if unit.kind is not request.kind:
            return self._answer(
                decision,
                request,
                status=EnumInstallRequestStatus.REFUSED,
                reason="kind_mismatch",
            )
        repository = next(
            (r for r in unit.repositories if r.name == request.repository), None
        )
        if repository is None:
            return self._answer(
                decision,
                request,
                status=EnumInstallRequestStatus.REFUSED,
                reason="repository_not_allowed",
            )
        verify = next((v for v in unit.verifies if v.name == request.verify), None)
        if verify is None:
            return self._answer(
                decision,
                request,
                status=EnumInstallRequestStatus.REFUSED,
                reason="verify_not_declared",
            )

        if unit.prepare_argv:
            argv = [
                arg.replace("{repository}", request.repository).replace(
                    "{ref}", request.ref
                )
                for arg in unit.prepare_argv
            ]
            done = self.runner(argv, self.overlay.command_timeout_seconds)
            if done.returncode != 0:
                return self._answer(
                    decision,
                    request,
                    status=EnumInstallRequestStatus.INSTALL_FAILED,
                    reason="prepare_failed",
                    detail=self._tail(done.stderr or done.stdout),
                )

        sha = self._resolve_commit(repository, request.ref)
        if sha is None:
            return self._answer(
                decision,
                request,
                status=EnumInstallRequestStatus.REFUSED,
                reason="ref_unresolved",
            )
        if not self._merged(repository, sha):
            return self._answer(
                decision,
                request,
                status=EnumInstallRequestStatus.REFUSED,
                reason="ref_not_merged",
            )

        installed = self.journal.latest_verified(self.executor_host, request.unit)
        if installed is not None and installed.revision == sha:
            # Already at the resolved commit: nothing installs, only the verify runs.
            return self._verify_only(decision, request, verify, sha)
        if (
            installed is not None
            and not request.rollback
            and self._is_ancestor(repository, sha, installed.revision)
        ):
            return self._answer(
                decision,
                request,
                status=EnumInstallRequestStatus.REFUSED,
                reason="older_than_installed",
                detail=(
                    f"{sha} is older than the installed {installed.revision};"
                    " only a rollback request goes back"
                ),
            )

        revision = ""
        if unit.kind is EnumInstallTarget.UNIT_TREE:
            if self.unit_tree_installer is None:
                raise ValueError(
                    "no unit tree installer was given, so a unit-tree request"
                    " cannot be installed"
                )
            tree = self.unit_tree_installer.handle(
                ModelUnitTreeInstallRequest(
                    operation="install",
                    unit=request.unit,
                    host=self.executor_host,
                    ref=sha,
                )
            )
            if tree.verdict != "pass":
                # The unit tree installer rolls its own tree back when its checks fail.
                return self._answer(
                    decision,
                    request,
                    status=EnumInstallRequestStatus.INSTALL_FAILED,
                    reason=f"installer_{tree.verdict}",
                    detail=self._tail(tree.detail),
                )
            revision = tree.commit
        else:
            argv = [
                arg.replace("{ref}", request.ref).replace("{sha}", sha)
                for arg in unit.installer_argv
            ]
            done = self.runner(argv, self.overlay.command_timeout_seconds)
            if done.returncode != 0:
                return self._answer(
                    decision,
                    request,
                    status=EnumInstallRequestStatus.INSTALL_FAILED,
                    reason="installer_failed",
                    detail=self._tail(done.stdout + done.stderr),
                )
            if unit.revision_pattern:
                found = re.search(unit.revision_pattern, done.stdout)
                revision = (
                    found.group(1) if found is not None and found.groups() else ""
                )

        if revision != sha:
            rolled_back, rollback_detail = self._rollback(request, unit, installed)
            return self._answer(
                decision,
                request,
                status=EnumInstallRequestStatus.INSTALL_FAILED,
                reason="revision_mismatch",
                detail=(
                    f"the installer reports {revision or 'no revision'},"
                    f" not {sha}; rollback: {rollback_detail}"
                ),
                rolled_back=rolled_back,
            )

        verified, reason, line, verify_detail = self._run_verify(verify, sha)
        if verified:
            return self._answer(
                decision,
                request,
                status=EnumInstallRequestStatus.VERIFIED,
                reason="ok",
                revision=sha,
                verify_line=line[: self.overlay.output_max_chars],
            )
        rolled_back, rollback_detail = self._rollback(request, unit, installed)
        return self._answer(
            decision,
            request,
            status=EnumInstallRequestStatus.VERIFY_FAILED,
            reason=reason,
            revision=sha,
            detail=f"{verify_detail}; rollback: {rollback_detail}",
            rolled_back=rolled_back,
        )

    # ------------------------------------------------------- the mirror reads

    def _git(
        self, repository: ModelInstallRepositoryRow, args: Sequence[str]
    ) -> subprocess.CompletedProcess[str]:
        """Run one git read against the host's own mirror clone of ``repository``."""
        argv = [
            *self.overlay.git_command,
            "-C",
            repository.mirror_path,
            *args,
        ]
        return self.runner(argv, self.overlay.command_timeout_seconds)

    def _resolve_commit(
        self, repository: ModelInstallRepositoryRow, ref: str
    ) -> str | None:
        """The full commit sha the host's own mirror resolves ``ref`` to; None when it resolves to none.

        A ref of only hex characters reaches the mirror as it is; any other ref
        is read through the repository row's own remote ref prefix. Nothing but
        the sha the mirror answers is installed from here on.
        """
        name = ref if _SHA.fullmatch(ref) else repository.remote_ref_prefix + ref
        done = self._git(
            repository,
            ["rev-parse", "--verify", "--quiet", name + "^{commit}"],
        )
        if done.returncode != 0:
            return None
        return done.stdout.strip()

    def _merged(self, repository: ModelInstallRepositoryRow, sha: str) -> bool:
        """Whether the mirror holds ``sha`` as an ancestor of the landing branch or a review branch."""
        branches = (repository.landing_branch, *repository.review_branches)
        return any(
            self._is_ancestor(repository, sha, repository.remote_ref_prefix + branch)
            for branch in branches
        )

    def _is_ancestor(
        self, repository: ModelInstallRepositoryRow, sha: str, revision: str
    ) -> bool:
        """Whether the mirror reads ``sha`` as an ancestor of ``revision``."""
        return (
            self._git(
                repository, ["merge-base", "--is-ancestor", sha, revision]
            ).returncode
            == 0
        )

    # ------------------------------------------------------ install's answers

    def _verify_only(
        self,
        decision: ModelInstallRequestDecision,
        request: ModelInstallRequest,
        verify: ModelInstallVerifyRow,
        sha: str,
    ) -> ModelInstallRequestHandled:
        """Answer the request whose commit is already installed: the verify alone runs."""
        verified, reason, line, verify_detail = self._run_verify(verify, sha)
        if verified:
            return self._answer(
                decision,
                request,
                status=EnumInstallRequestStatus.VERIFIED,
                reason="already_installed",
                revision=sha,
                verify_line=line[: self.overlay.output_max_chars],
            )
        return self._answer(
            decision,
            request,
            status=EnumInstallRequestStatus.VERIFY_FAILED,
            reason=reason,
            revision=sha,
            # Nothing was reinstalled, so there is nothing to roll back.
            detail=f"already installed at {sha}; {verify_detail}",
        )

    def _run_verify(
        self, verify: ModelInstallVerifyRow, sha: str
    ) -> tuple[bool, str, str, str]:
        """Run the declared verify: ``(verified, reason, line, detail)``.

        The argv and the expected line are the overlay row's; ``{revision}`` in
        the expected line reads the resolved sha. Verified only when the command
        exits 0 and one line of its stdout holds the expected line.
        """
        done = self.runner(list(verify.argv), self.overlay.command_timeout_seconds)
        expected = verify.expected_line.replace("{revision}", sha)
        line = next(
            (out.strip() for out in done.stdout.splitlines() if expected in out),
            "",
        )
        if done.returncode == 0 and line:
            return True, "ok", line, ""
        detail = f"exit {done.returncode}: {self._tail(done.stdout + done.stderr)}"
        return (
            False,
            "verify_mismatch" if done.returncode == 0 else "verify_failed",
            "",
            detail,
        )

    def _rollback(
        self,
        request: ModelInstallRequest,
        unit: ModelInstallUnitRow,
        previous: ModelInstallRequestResult | None,
    ) -> tuple[bool, str]:
        """Try to restore the previous verified revision: ``(restored, detail)``.

        With no previous revision nothing is restored and the detail says so. A
        unit tree rolls back through its own installer; a plugin tree runs the
        overlay's declared rollback argv with ``{ref}`` the previous revision.
        """
        if previous is None:
            return False, "no prior revision to restore"
        if unit.kind is EnumInstallTarget.UNIT_TREE:
            if self.unit_tree_installer is None:
                raise ValueError(
                    "no unit tree installer was given, so a unit-tree request"
                    " cannot roll back"
                )
            restored = self.unit_tree_installer.handle(
                ModelUnitTreeInstallRequest(
                    operation="install",
                    unit=request.unit,
                    host=self.executor_host,
                    ref=previous.revision,
                )
            )
            if restored.verdict != "pass":
                return (
                    False,
                    f"restore of {previous.revision} failed:"
                    f" {self._tail(restored.detail)}",
                )
            return True, f"restored {previous.revision}"
        argv = [arg.replace("{ref}", previous.revision) for arg in unit.rollback_argv]
        done = self.runner(argv, self.overlay.command_timeout_seconds)
        if done.returncode != 0:
            return (
                False,
                f"restore of {previous.revision} failed:"
                f" {self._tail(done.stderr or done.stdout)}",
            )
        return True, f"restored {previous.revision}"

    # ----------------------------------------------------------------- plumbing

    def _decide(
        self,
        received: ModelInstallRequestReceived,
        row: ModelInstallHostRow,
        blockers: tuple[str, ...],
    ) -> ModelInstallRequestDecision:
        return self._decision.handle(
            ModelInstallRequestDecisionInput(
                request=received.request,
                executor_host=self.executor_host,
                now=self.now(),
                prior_result=self.journal.get(received.request.request_id),
                blockers=blockers,
                publisher=received.publisher,
                allowed_publishers=row.allowed_publishers,
                max_age_seconds=self.overlay.max_command_age_seconds,
            )
        )

    def _answer(
        self,
        decision: ModelInstallRequestDecision,
        request: ModelInstallRequest,
        *,
        status: EnumInstallRequestStatus,
        reason: str,
        revision: str = "",
        verify_line: str = "",
        detail: str = "",
        rolled_back: bool = False,
        journal: bool = True,
    ) -> ModelInstallRequestHandled:
        """Build one result for this host and request, journal it, wrap it.

        ``journal`` is False only for a publisher refusal: that answer is
        published but never written to the journal.
        """
        result = ModelInstallRequestResult(
            request_id=request.request_id,
            host=self.executor_host,
            unit=request.unit,
            status=status,
            at=self.now(),
            revision=revision,
            verify_line=verify_line,
            reason=reason,
            detail=detail,
            rolled_back=rolled_back,
        )
        if journal:
            self.journal.append(result)
        return ModelInstallRequestHandled(decision=decision, result=result)

    def _tail(self, text: str) -> str:
        return text[-self.overlay.output_max_chars :]


__all__ = [
    "HandlerInstallRequest",
    "InMemoryResultJournal",
    "JsonlResultJournal",
    "ProtocolResultJournal",
    "ProtocolUnitTreeInstaller",
    "Runner",
    "default_runner",
]
