# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler of node_code_contract_score_effect (OMN-23626).

A code draft is scored by the ticket contract's own bound checks run against the draft. The delegation gate's text
criteria are recorded beside the verdict (``gate_accepted``) and are never the score.

One request, one verdict:

1. the contract is read at its named revision and its sha256 is verified (a mismatch refuses the request);
2. each check file is read at its revision and verified by sha256 (a mismatch refuses the request);
3. a draft that edits the contract, a check file or a protected path is ``fail_tampered`` before anything is applied;
4. a scratch worktree is made at the base commit, the draft is applied (``fail_did_not_apply``), the check files are
   placed over it and the environment is synced (timed apart from the checks);
5. each bound check of type ``test_passes`` or ``command`` runs; any failure is ``fail_check``, except a pytest run that
   ended on exit 1 with an error and no failed test in its summary line (a fixture that cannot reach its service): that is
   a fault at check setup and is ``unscorable(env)``. A collection error (pytest exit 2) stays a failure of the draft;
6. a draft whose checks all pass is read again at the base with only the check files placed: a check that does not fail
   there is an invalid falsifier and the verdict is ``unscorable(falsifier_not_held)``, never a pass.

An infrastructure fault (contract, environment, worktree, runner) is ``unscorable`` with its own source, never a fail and
never a pass (the rule ``node_delegated_test_loop_orchestrator`` states). The check runner is the verifier's own
environment scrub and failure reader (node_dod_verify) over a scratch worktree; the verifier's collector resolves its cwd from
ticket and pull request placeholders under the registry root and cannot run in a scratch worktree.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from pathlib import Path

import yaml
from omnibase_core.enums.ticket.enum_dod_evidence_execution_scope import (
    EnumDodEvidenceExecutionScope,
)
from omnibase_core.models.ticket.model_ticket_contract import ModelTicketContract
from pydantic import ValidationError

from omnimarket.models.model_delegated_test_control import (
    EnumControlStatus,
    ModelControlGradeRequest,
)
from omnimarket.models.model_delegated_test_control import (
    RunOutcome as ControlRunOutcome,
)
from omnimarket.nodes.contract_topics import contract_topic
from omnimarket.nodes.node_code_contract_score_effect.handlers.adapter_git_workspace import (
    BashCheckExecution,
    GitWorkspace,
)
from omnimarket.nodes.node_code_contract_score_effect.handlers.parser_pytest_summary import (
    is_setup_error_run,
    parse_pytest_summary,
)
from omnimarket.nodes.node_code_contract_score_effect.models import (
    EnumCodeScoreCheckOutcome,
    EnumCodeScoreFailureCode,
    EnumCodeScoreRuntime,
    EnumCodeScoreSource,
    EnumCodeScoreVerdict,
    ModelCodeScoreCheckResult,
    ModelCodeScored,
    ModelCodeScoreFailed,
    ModelCodeScoreRequest,
    ModelCodeScoreResult,
    ModelCodeScoreTimings,
    ModelCodeScoreVerdict,
)
from omnimarket.nodes.node_code_contract_score_effect.protocols import (
    ProtocolCodeScoreEventPublisher,
    ProtocolCodeScoreRunner,
    ProtocolCodeScoreWorkspace,
    WorkspaceError,
)
from omnimarket.nodes.node_delegated_test_control_compute.handlers.handler_delegated_test_control import (
    grade_control,
)

_log = logging.getLogger(__name__)
_CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"
_DIFF_GIT = re.compile(r"^diff --git a/(\S+) b/(\S+)$", re.MULTILINE)
_DIFF_FILE = re.compile(r"^(?:---|\+\+\+) (?:a|b)/(\S+)$", re.MULTILINE)
_DIFF_RENAME = re.compile(r"^rename (?:from|to) (\S+)$", re.MULTILINE)


class _RefusedError(Exception):
    """Internal: the request is refused before any verdict."""

    def __init__(self, code: EnumCodeScoreFailureCode, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


class _UnscorableError(Exception):
    """Internal: an infrastructure fault; the draft is neither failed nor passed."""

    def __init__(self, source: EnumCodeScoreSource, detail: str) -> None:
        super().__init__(detail)
        self.source = source
        self.detail = detail


def sha256_hex(data: bytes) -> str:
    """The sha256 the request pins file contents with."""
    return hashlib.sha256(data).hexdigest()


def diff_paths(diff: str) -> frozenset[str]:
    """Every path a unified diff touches (both sides of a rename, both file headers)."""
    paths: set[str] = set()
    for old, new in _DIFF_GIT.findall(diff):
        paths.update((old, new))
    paths.update(_DIFF_FILE.findall(diff))
    paths.update(_DIFF_RENAME.findall(diff))
    return frozenset(paths)


def _prefix_outcome(
    results: tuple[ModelCodeScoreCheckResult, ...],
    refused: tuple[EnumCodeScoreVerdict, str] | None,
) -> ControlRunOutcome:
    """The control grader's outcome word for the base run: a bound check that failed there is ``failed_call``."""
    if refused is not None:
        return "infra_error"
    if any(r.outcome is EnumCodeScoreCheckOutcome.FAILED for r in results):
        return "failed_call"
    return "passed"


def _is_protected(path: str, protected: tuple[str, ...]) -> bool:
    return any(
        path == entry or path.startswith(entry if entry.endswith("/") else entry + "/")
        for entry in protected
    )


class HandlerCodeContractScore:
    """EFFECT: score one code draft by the ticket contract's own bound checks."""

    def __init__(
        self,
        workspace: ProtocolCodeScoreWorkspace | None = None,
        runner: ProtocolCodeScoreRunner | None = None,
        event_bus: ProtocolCodeScoreEventPublisher | None = None,
    ) -> None:
        self._workspace: ProtocolCodeScoreWorkspace = workspace or GitWorkspace()
        self._runner: ProtocolCodeScoreRunner = runner or BashCheckExecution()
        self._event_bus = event_bus
        self._scored_topic = contract_topic(
            _CONTRACT_PATH, "publish_topics", "scored.v1"
        )
        self._failed_topic = contract_topic(
            _CONTRACT_PATH, "publish_topics", "failed.v1"
        )

    async def handle(self, request: ModelCodeScoreRequest) -> ModelCodeScoreResult:
        """Score one draft; a refusal ends on the failed event, a fault on an unscorable verdict."""
        try:
            result = self._score(request)
        except _RefusedError as refusal:
            result = ModelCodeScoreResult(
                failed=ModelCodeScoreFailed(
                    correlation_id=request.correlation_id,
                    code=refusal.code,
                    detail=refusal.detail,
                )
            )
        except Exception as error:
            _log.error("code contract score %s failed", request.correlation_id)
            result = ModelCodeScoreResult(
                failed=ModelCodeScoreFailed(
                    correlation_id=request.correlation_id,
                    code=EnumCodeScoreFailureCode.INTERNAL_ERROR,
                    detail=f"the score could not run ({type(error).__name__})",
                )
            )
        await self._publish(request, result)
        return result

    async def _publish(
        self, request: ModelCodeScoreRequest, result: ModelCodeScoreResult
    ) -> None:
        if self._event_bus is None:
            return
        event = result.scored or result.failed
        topic = self._scored_topic if result.scored else self._failed_topic
        if event is None:
            return
        try:
            await self._event_bus.publish(
                topic,
                key=str(request.correlation_id).encode(),
                value=event.model_dump_json().encode(),
            )
        except Exception:
            _log.exception("code contract score: failed to publish on %s", topic)

    # --- the score -------------------------------------------------------------------------------------------

    def _score(self, request: ModelCodeScoreRequest) -> ModelCodeScoreResult:
        timings = {"worktree_s": 0.0, "env_sync_s": 0.0, "checks_s": 0.0}
        files = self._read_pinned_files(request)
        try:
            verdict = self._verdict(request, files, timings)
        except _UnscorableError as fault:
            verdict = ModelCodeScoreVerdict(
                checks=EnumCodeScoreVerdict.UNSCORABLE,
                source=fault.source,
                detail=fault.detail,
            )
        return ModelCodeScoreResult(
            scored=ModelCodeScored(
                correlation_id=request.correlation_id,
                repo=request.repo,
                base_commit=request.base_commit,
                draft_diff_sha256=sha256_hex(request.draft_diff.encode()),
                try_identity=request.try_identity,
                gate_accepted=request.gate_accepted,
                verdict=verdict,
                timings=ModelCodeScoreTimings(**timings),
            )
        )

    def _read_pinned_files(self, request: ModelCodeScoreRequest) -> dict[str, bytes]:
        """The contract and each check file at their revisions, each verified by sha256."""
        pinned: dict[str, bytes] = {}
        contract_root = request.overlay.repo_roots[request.contract.repository]
        text = self._workspace.read_blob(
            contract_root, request.contract.revision, request.contract.path
        )
        if text is not None and sha256_hex(text) != request.contract.sha256:
            raise _RefusedError(
                EnumCodeScoreFailureCode.CONTRACT_SHA256_MISMATCH,
                f"contract {request.contract.path} does not match its sha256",
            )
        if text is not None:
            pinned[request.contract.path] = text
        root = request.overlay.repo_roots[request.repo]
        for check_file in request.check_files:
            data = self._workspace.read_blob(root, check_file.revision, check_file.path)
            if data is None:
                raise _RefusedError(
                    EnumCodeScoreFailureCode.CHECK_FILE_UNREADABLE,
                    f"check file {check_file.path} is not readable at its revision",
                )
            if sha256_hex(data) != check_file.sha256:
                raise _RefusedError(
                    EnumCodeScoreFailureCode.CHECK_FILE_SHA256_MISMATCH,
                    f"check file {check_file.path} does not match its sha256",
                )
            pinned[check_file.path] = data
        return pinned

    def _verdict(
        self,
        request: ModelCodeScoreRequest,
        files: dict[str, bytes],
        timings: dict[str, float],
    ) -> ModelCodeScoreVerdict:
        checks = self._bound_checks(request, files.get(request.contract.path))
        tampered = self._tampered_paths(request)
        if tampered:
            return ModelCodeScoreVerdict(
                checks=EnumCodeScoreVerdict.FAIL_TAMPERED,
                detail="the draft edits " + ", ".join(sorted(tampered)),
            )
        results, outcome = self._run_at(
            request,
            files,
            checks,
            request.draft_diff,
            self._scoring_commit(request),
            timings,
        )
        if outcome is not None:
            return ModelCodeScoreVerdict(
                checks=outcome[0], detail=outcome[1], check_results=results
            )
        if any(r.outcome is EnumCodeScoreCheckOutcome.FAILED for r in results):
            return ModelCodeScoreVerdict(
                checks=EnumCodeScoreVerdict.FAIL_CHECK, check_results=results
            )
        # Every bound check passed. The must-fail control is graded by node_delegated_test_control_compute: the checks
        # read again at the base with only the check files placed must fail there, or they do not discriminate.
        base_results, base_outcome = self._run_at(
            request, files, checks, "", request.base_commit, timings
        )
        grade = grade_control(
            ModelControlGradeRequest(
                fixed_outcome="passed",
                prefix_outcome=_prefix_outcome(base_results, base_outcome),
                mutation_requested=False,
                prefix_ref_equals_fixed_ref=(
                    self._scoring_commit(request) == request.base_commit
                    and not request.draft_diff.strip()
                ),
            )
        )
        if grade.status is EnumControlStatus.INFRA_ERROR:
            return ModelCodeScoreVerdict(
                checks=EnumCodeScoreVerdict.UNSCORABLE,
                source=EnumCodeScoreSource.HARNESS,
                detail=grade.reason,
                check_results=results,
            )
        if not grade.headline:
            return ModelCodeScoreVerdict(
                checks=EnumCodeScoreVerdict.UNSCORABLE,
                source=EnumCodeScoreSource.FALSIFIER_NOT_HELD,
                detail=f"the bound checks do not fail at the base commit with the check files placed ({grade.reason})",
                check_results=results,
            )
        return ModelCodeScoreVerdict(
            checks=EnumCodeScoreVerdict.PASS,
            runtime=EnumCodeScoreRuntime.UNREAD_EVAL,
            check_results=results,
        )

    def _scoring_commit(self, request: ModelCodeScoreRequest) -> str:
        """The commit the draft is applied on: always the request's base commit."""
        return request.base_commit

    def _bound_checks(
        self, request: ModelCodeScoreRequest, contract_text: bytes | None
    ) -> list[tuple[str, str, str]]:
        """(item id, check type, command) of every runnable bound check; a fault is unscorable."""
        if contract_text is None:
            raise _UnscorableError(
                EnumCodeScoreSource.CONTRACT,
                f"no contract at {request.contract.path} at its revision",
            )
        try:
            contract = ModelTicketContract.model_validate(yaml.safe_load(contract_text))
        except (yaml.YAMLError, ValidationError, TypeError) as error:
            raise _UnscorableError(
                EnumCodeScoreSource.CONTRACT,
                f"the contract is unreadable ({type(error).__name__})",
            ) from error
        wanted = set(request.check_item_ids)
        known = {item.id for item in contract.dod_evidence}
        if wanted - known:
            raise _UnscorableError(
                EnumCodeScoreSource.CONTRACT,
                "the contract has no dod_evidence item "
                + ", ".join(sorted(wanted - known)),
            )
        runnable = set(request.overlay.runnable_check_types)
        bound: list[tuple[str, str, str]] = []
        for item in contract.dod_evidence:
            if wanted and item.id not in wanted:
                continue
            if item.execution_scope is EnumDodEvidenceExecutionScope.LOCAL_DONE_GATE:
                continue
            for check in item.checks:
                if check.check_type.value in runnable and isinstance(
                    check.check_value, str
                ):
                    bound.append((item.id, check.check_type.value, check.check_value))
        if not bound:
            raise _UnscorableError(
                EnumCodeScoreSource.CHECK_NOT_RUNNABLE,
                "the contract binds no check of a type runnable in a worktree",
            )
        return bound

    def _tampered_paths(self, request: ModelCodeScoreRequest) -> frozenset[str]:
        protected = request.overlay.protected_paths.get(request.repo, ())
        pinned = {request.contract.path, *(f.path for f in request.check_files)}
        return frozenset(
            path
            for path in diff_paths(request.draft_diff)
            if path in pinned or _is_protected(path, protected)
        )

    def _run_at(
        self,
        request: ModelCodeScoreRequest,
        files: dict[str, bytes],
        checks: list[tuple[str, str, str]],
        diff: str,
        commit: str,
        timings: dict[str, float],
    ) -> tuple[
        tuple[ModelCodeScoreCheckResult, ...], tuple[EnumCodeScoreVerdict, str] | None
    ]:
        """Make a worktree at ``commit``, apply ``diff``, place the check files, sync, run the checks."""
        root = request.overlay.repo_roots[request.repo]
        started = time.monotonic()
        try:
            worktree = self._workspace.create(
                root, commit, request.overlay.scratch_root
            )
        except WorkspaceError as error:
            raise _UnscorableError(
                EnumCodeScoreSource.WORKTREE, f"no worktree ({error})"
            ) from error
        timings["worktree_s"] += time.monotonic() - started
        try:
            if diff.strip():
                reason = self._workspace.apply_diff(worktree, diff)
                if reason is not None:
                    return (), (EnumCodeScoreVerdict.FAIL_DID_NOT_APPLY, reason)
            self._place_checks(request, files, worktree)
            return self._sync_and_run(request, checks, worktree, timings)
        finally:
            self._workspace.remove(root, worktree)

    def _place_checks(
        self, request: ModelCodeScoreRequest, files: dict[str, bytes], worktree: Path
    ) -> None:
        for check_file in request.check_files:
            try:
                self._workspace.place_file(
                    worktree, check_file.path, files[check_file.path]
                )
            except WorkspaceError as error:
                raise _UnscorableError(
                    EnumCodeScoreSource.WORKTREE, str(error)
                ) from error

    def _sync_and_run(
        self,
        request: ModelCodeScoreRequest,
        checks: list[tuple[str, str, str]],
        worktree: Path,
        timings: dict[str, float],
    ) -> tuple[
        tuple[ModelCodeScoreCheckResult, ...], tuple[EnumCodeScoreVerdict, str] | None
    ]:
        overlay = request.overlay
        sync = self._runner.run(
            overlay.env_sync_command, worktree, overlay.env_sync_timeout_s
        )
        timings["env_sync_s"] += sync.duration_s
        if sync.timed_out or sync.exit_code != 0:
            raise _UnscorableError(
                EnumCodeScoreSource.ENV,
                "the environment sync "
                + ("timed out" if sync.timed_out else f"exited {sync.exit_code}"),
            )
        results: list[ModelCodeScoreCheckResult] = []
        for item_id, check_type, command in checks:
            ran = self._runner.run(command, worktree, overlay.check_timeout_s)
            timings["checks_s"] += ran.duration_s
            if ran.timed_out:
                raise _UnscorableError(
                    EnumCodeScoreSource.HARNESS,
                    f"check {item_id} hit the {overlay.check_timeout_s} s limit",
                )
            if is_setup_error_run(ran.exit_code, ran.output):
                summary = parse_pytest_summary(ran.output)
                errors = summary.errors if summary else 0
                raise _UnscorableError(
                    EnumCodeScoreSource.ENV,
                    f"check {item_id} ended in a pytest setup error with no failed test ({errors} error)",
                )
            results.append(
                ModelCodeScoreCheckResult(
                    item_id=item_id,
                    check_type=check_type,
                    command=command,
                    outcome=(
                        EnumCodeScoreCheckOutcome.PASSED
                        if ran.exit_code == 0
                        else EnumCodeScoreCheckOutcome.FAILED
                    ),
                    exit_code=ran.exit_code,
                    output=ran.output,
                )
            )
        return tuple(results), None


__all__ = ["HandlerCodeContractScore", "diff_paths", "sha256_hex"]
