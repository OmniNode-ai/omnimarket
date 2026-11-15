# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Execute one install request on the host this handler serves (OMN-19998, root 5).

The effect half of the install request: read this host's own overlay row (the
deployment names what may be installed, a request cannot widen it), decide with
node_install_request_decision_compute, and only then touch the host. A request
for another host, one already verified and one whose host is not safe yet is
answered by its decision alone; an expired one is answered ``expired`` without
running anything; a request the overlay row does not cover is refused with a
typed reason before the first command runs. What does run is the declared
prepare command, the declared installer (node_unit_tree_install_effect for a
unit tree, the overlay's installer argv for the plugin tree) and the request's
own verify command, each under the overlay's timeout. Every result is appended
to the journal (the request id is the idempotency key) and returned for
publication.

Every setting is an overlay value: no timeout, path, prefix or host name lives
in this file. The only literals are the sha length bounds (7 and 40 characters)
that decide whether a ref is a commit sha or a branch name.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable, Sequence
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
    ModelInstallRequestEffectOverlay,
    ModelInstallUnitRow,
)
from omnimarket.nodes.node_unit_tree_install_effect.models.model_unit_tree_install import (
    ModelUnitTreeInstallRequest,
    ModelUnitTreeInstallResult,
)

#: A ref that is entirely 7 to 40 lowercase hex characters is a commit sha and
#: reaches the unit tree installer as it is; anything else is a branch name and
#: is read through the unit row's own branch ref prefix.
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
    """The results already published, keyed by request id."""

    def get(self, request_id: UUID) -> ModelInstallRequestResult | None: ...

    def append(self, result: ModelInstallRequestResult) -> None: ...


class InMemoryResultJournal(dict[UUID, ModelInstallRequestResult]):
    """The journal of one process: the last result per request id."""

    def append(self, result: ModelInstallRequestResult) -> None:
        self[result.request_id] = result


class JsonlResultJournal:
    """One JSON line per result, appended to the file; a missing file reads empty.

    ``get`` returns the last line the id owns, except that a verified result
    wins over any other line for the same id: once a request is verified it
    stays verified, whatever a later re-run wrote. An unreadable line raises --
    the journal is the idempotency key, and a swallowed read error would
    silently re-run an install.
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    def append(self, result: ModelInstallRequestResult) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as stream:
            stream.write(result.model_dump_json() + "\n")

    def get(self, request_id: UUID) -> ModelInstallRequestResult | None:
        if not self._path.is_file():
            return None
        last: ModelInstallRequestResult | None = None
        verified: ModelInstallRequestResult | None = None
        for line in self._path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            result = ModelInstallRequestResult.model_validate_json(line)
            if result.request_id != request_id:
                continue
            last = result
            if result.status is EnumInstallRequestStatus.VERIFIED:
                verified = result
        return verified if verified is not None else last


class HandlerInstallRequest:
    """``handle(request) -> ModelInstallRequestHandled``: decide, install, verify, answer."""

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

    def handle(self, request: ModelInstallRequest) -> ModelInstallRequestHandled:
        row = self.overlay.host(self.executor_host)
        if row is None:
            raise ValueError(
                f"{self.executor_host} is no host in the install overlay;"
                " the deployment named a host this effect cannot serve"
            )
        blockers = tuple(
            f"lock:{path}" for path in row.blocker_files if Path(path).exists()
        )
        decision = self._decide(request, blockers)
        if decision.action in (
            EnumInstallRequestAction.IGNORE,
            EnumInstallRequestAction.SKIP_DONE,
            EnumInstallRequestAction.HOLD,
        ):
            return ModelInstallRequestHandled(decision=decision, result=None)
        if decision.action is EnumInstallRequestAction.EXPIRE:
            return self._answer(
                decision,
                request,
                status=EnumInstallRequestStatus.EXPIRED,
                reason="expired",
            )
        return self._execute(decision, request, row)

    # ------------------------------------------------------------------ execute

    def _execute(
        self,
        decision: ModelInstallRequestDecision,
        request: ModelInstallRequest,
        row: ModelInstallHostRow,
    ) -> ModelInstallRequestHandled:
        """Refuse what the host's overlay row does not cover, then install and verify."""
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
        if request.repository not in unit.repositories:
            return self._answer(
                decision,
                request,
                status=EnumInstallRequestStatus.REFUSED,
                reason="repository_not_allowed",
            )
        if not self._verify_command_declared(unit, request.verify_command):
            return self._answer(
                decision,
                request,
                status=EnumInstallRequestStatus.REFUSED,
                reason="verify_command_not_declared",
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

        revision = ""
        if unit.kind is EnumInstallTarget.UNIT_TREE:
            if self.unit_tree_installer is None:
                raise ValueError(
                    "no unit tree installer was given, so a unit-tree request"
                    " cannot be installed"
                )
            ref = (
                request.ref
                if _SHA.fullmatch(request.ref)
                else unit.branch_ref_prefix + request.ref
            )
            installed = self.unit_tree_installer.handle(
                ModelUnitTreeInstallRequest(
                    operation="install",
                    unit=request.unit,
                    host=self.executor_host,
                    ref=ref,
                )
            )
            if installed.verdict != "pass":
                return self._answer(
                    decision,
                    request,
                    status=EnumInstallRequestStatus.INSTALL_FAILED,
                    reason=f"installer_{installed.verdict}",
                    detail=self._tail(installed.detail),
                )
            revision = installed.commit
        else:
            argv = [arg.replace("{ref}", request.ref) for arg in unit.installer_argv]
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

        done = self.runner(
            list(request.verify_command), self.overlay.command_timeout_seconds
        )
        line = next(
            (
                out.strip()
                for out in done.stdout.splitlines()
                if request.expected_line in out
            ),
            "",
        )
        if done.returncode == 0 and line:
            return self._answer(
                decision,
                request,
                status=EnumInstallRequestStatus.VERIFIED,
                reason="ok",
                revision=revision,
                verify_line=line[: self.overlay.output_max_chars],
            )
        return self._answer(
            decision,
            request,
            status=EnumInstallRequestStatus.VERIFY_FAILED,
            reason="verify_mismatch" if done.returncode == 0 else "verify_failed",
            revision=revision,
            detail=(f"exit {done.returncode}: {self._tail(done.stdout + done.stderr)}"),
        )

    def _verify_command_declared(
        self, unit: ModelInstallUnitRow, verify_command: Sequence[str]
    ) -> bool:
        """Whether the verify command starts with one of the unit's argv prefixes."""
        command = tuple(verify_command)
        return any(
            command[: len(prefix)] == tuple(prefix) for prefix in unit.verify_prefixes
        )

    def _decide(
        self, request: ModelInstallRequest, blockers: tuple[str, ...]
    ) -> ModelInstallRequestDecision:
        return self._decision.handle(
            ModelInstallRequestDecisionInput(
                request=request,
                executor_host=self.executor_host,
                now=self.now(),
                prior_result=self.journal.get(request.request_id),
                blockers=blockers,
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
    ) -> ModelInstallRequestHandled:
        """Build one result for this host and request, journal it, wrap it."""
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
        )
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
