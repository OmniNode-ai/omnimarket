# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run only the contract's participant onboarding commands on a stock Tart VM."""

from __future__ import annotations

import re
import shlex
import subprocess
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import yaml
from pydantic import BaseModel, Field

from omnimarket.nodes.node_onboarding_clean_vm_walk_effect.handlers.guest_ssh import (
    bind_guest_ip,
    guest_ssh_argv,
    require_guest_login,
)
from omnimarket.nodes.node_onboarding_clean_vm_walk_effect.handlers.receipt_redaction import (
    redact,
    sanitize_receipt,
)
from omnimarket.nodes.node_onboarding_clean_vm_walk_effect.handlers.ssh_tart_runner import (
    HandlerSshTart,
)
from omnimarket.nodes.node_onboarding_clean_vm_walk_effect.handlers.walk_log_parser import (
    parse_delegated_run,
    parse_macos_version,
    parse_phases,
    parse_pins,
    seconds_to_first_delegated_run,
)
from omnimarket.nodes.node_onboarding_clean_vm_walk_effect.models.model_clean_vm_walk import (
    EnumPhaseResult,
    EnumWalkOutcome,
    EnumWalkStepKind,
    ModelDashboardResult,
    ModelDelegatedRun,
    ModelInstalledPin,
    ModelPhaseRecord,
    ModelRunnerResult,
    ModelVmSpec,
    ModelWalkReceipt,
    ModelWalkRequest,
    ModelWalkStep,
    ModelWalkStepRecord,
    StepNotDocumentedError,
)
from omnimarket.nodes.node_onboarding_clean_vm_walk_effect.protocols.protocol_tart_runner import (
    ProtocolTartRunner,
)

_CONTRACT = Path(__file__).resolve().parent.parent / "contract.yaml"
_OPERATION_ERRORS = (
    OSError,
    ValueError,
    RuntimeError,
    subprocess.SubprocessError,
    yaml.YAMLError,
)
# What Tart says when a VM of that name is not on the host.
_VM_ABSENT = re.compile(r"(?i)does not exist|not found|no such")
# Exit codes that carry no Tart verdict: a transport timeout (124) or a signal or
# SSH failure (128 and up). The clone may have completed on the host.
_LOST_REPLY_FLOOR = 128
_TRANSPORT_TIMEOUT_EXIT = 124


def _clone_reply_lost(result: ModelRunnerResult) -> bool:
    """A failed clone with no verdict from Tart: the VM may exist, and the walk made it."""
    return (
        result.exit_code == _TRANSPORT_TIMEOUT_EXIT
        or result.exit_code >= _LOST_REPLY_FLOOR
    )


class _TartDefaults(BaseModel):
    stock_image: str = Field(min_length=1)
    cpu: int = Field(ge=1)
    memory_gb: int = Field(ge=1)
    tart_path: str = Field(min_length=1)
    guest_user: str = Field(min_length=1)


class _DeclaredStep(BaseModel):
    kind: EnumWalkStepKind
    description: str


class _WalkContract(BaseModel):
    tart: _TartDefaults
    documented_steps: tuple[_DeclaredStep, ...]


def _load_contract() -> _WalkContract:
    return _WalkContract.model_validate(yaml.safe_load(_CONTRACT.read_text()))


def resolve_request(request: ModelWalkRequest) -> ModelWalkRequest:
    """Resolve optional VM settings from the same contract that guards execution."""
    defaults = _load_contract().tart
    return request.model_copy(
        update={
            "image": request.image or defaults.stock_image,
            "cpu": request.cpu if request.cpu is not None else defaults.cpu,
            "memory_gb": request.memory_gb
            if request.memory_gb is not None
            else defaults.memory_gb,
            "tart_path": request.tart_path or defaults.tart_path,
            "guest_user": request.guest_user or defaults.guest_user,
            "vm_name": request.vm_name or f"onex-walk-{uuid4().hex[:8]}",
        }
    )


def _dashboard_probe(*, projections: bool) -> str:
    # The guest consumes the token without ever printing it or sending it to the host.
    # Each probe is one documented bash -lc step, including the bounded startup wait.
    target = '"$base/projections$query"' if projections else '"$url"'
    return (
        'url=""; for attempt in {1..30}; do '
        'if [ -s "$HOME/walk-dashboard.log" ]; then '
        'IFS= read -r url < "$HOME/walk-dashboard.log"; '
        '[ -n "$url" ] && break; fi; sleep 1; done; '
        '[ -n "$url" ] || exit 1; '
        'case "$url" in http://127.0.0.1:*|http://localhost:*|http://\\[::1\\]:*) ;; *) exit 1;; esac; '
        'base=${url%%\\?*}; base=${base%/}; query=""; '
        'case "$url" in *\\?*) query="?${url#*\\?}";; esac; '
        f"code=$(curl --max-time 30 -s -o /dev/null -w '%{{http_code}}' {target}) || exit 1; "
        'printf "%s %s\\n" "$code" "$base"'
    )


def _documented_argv(
    kind: EnumWalkStepKind, request: ModelWalkRequest
) -> tuple[str, ...]:
    """Canonical command specification, independent of the handler's public builder."""
    tart, vm = request.tart_path, request.vm_name
    if (
        tart is None
        or vm is None
        or request.image is None
        or request.cpu is None
        or request.memory_gb is None
    ):
        raise ValueError(
            "resolve the request from the contract before building commands"
        )
    host_commands = {
        EnumWalkStepKind.TART_CLONE: (tart, "clone", request.image, vm),
        EnumWalkStepKind.TART_SET_SPEC: (
            tart,
            "set",
            vm,
            "--cpu",
            str(request.cpu),
            "--memory",
            str(request.memory_gb * 1024),
        ),
        EnumWalkStepKind.TART_RUN: (tart, "run", "--no-graphics", vm),
        EnumWalkStepKind.TART_IP: (tart, "ip", vm, "--wait", "120"),
        EnumWalkStepKind.TART_STOP: (tart, "stop", vm),
        EnumWalkStepKind.TART_DELETE: (tart, "delete", vm),
    }
    if kind in host_commands:
        return host_commands[kind]
    flags = ["--provider", ",".join(request.providers)]
    for flag, value in (
        ("--local-url", request.local_url),
        ("--local-model", request.local_model),
        ("--ollama-model", request.ollama_model),
    ):
        if value is not None:
            flags.extend((flag, value))
    url = f"https://raw.githubusercontent.com/OmniNode-ai/omniclaude/{request.script_sha}/plugins/onex/skills/_bin/lab-onboarding.sh"
    guest_commands = {
        EnumWalkStepKind.GUEST_FETCH_SCRIPT: 'cd "$HOME" && mkdir -p "$HOME/walk" && cd "$HOME/walk" && '
        f"curl -fsSL -o lab-onboarding.sh {shlex.quote(url)}",
        EnumWalkStepKind.GUEST_VERIFY_CHECKSUM: 'cd "$HOME/walk" && '
        f"echo {shlex.quote(request.checksum_line)} | shasum -a 256 -c -",
        EnumWalkStepKind.GUEST_RUN_SCRIPT: 'cd "$HOME/walk" && bash lab-onboarding.sh '
        + " ".join(shlex.quote(flag) for flag in flags)
        + ' > "$HOME/walk-run.log" 2>&1',
        EnumWalkStepKind.GUEST_READ_LOG: 'cat "$HOME/walk-run.log"',
        EnumWalkStepKind.GUEST_DASHBOARD_START: 'nohup onex dashboard > "$HOME/walk-dashboard.log" 2>&1 & echo $! > "$HOME/walk-dashboard.pid"',
        EnumWalkStepKind.GUEST_DASHBOARD_PROBE_ROOT: _dashboard_probe(
            projections=False
        ),
        EnumWalkStepKind.GUEST_DASHBOARD_PROBE_PROJECTIONS: _dashboard_probe(
            projections=True
        ),
        EnumWalkStepKind.GUEST_DASHBOARD_STOP: 'kill $(cat "$HOME/walk-dashboard.pid")',
    }
    if kind not in guest_commands:
        raise StepNotDocumentedError(f"{kind}: step not documented")
    return guest_ssh_argv(request, guest_commands[kind])


def build_step_argv(
    kind: EnumWalkStepKind, request: ModelWalkRequest
) -> tuple[str, ...]:
    """Pure handler command builder; the guard checks the canonical specification."""
    return _documented_argv(kind, request)


class DocumentedStepGuard:
    """Refuse undeclared kinds and altered argv before invoking the transport."""

    def __init__(self, request: ModelWalkRequest, runner: ProtocolTartRunner) -> None:
        self._request = request
        self._runner = runner
        self._declared = frozenset(
            step.kind for step in _load_contract().documented_steps
        )

    def issue(self, step: ModelWalkStep) -> ModelRunnerResult:
        if step.kind not in self._declared or step.argv != _documented_argv(
            step.kind, self._request
        ):
            raise StepNotDocumentedError(
                f"{step.kind}: step not documented (kind or argv refused)"
            )
        if step.kind == EnumWalkStepKind.TART_RUN:
            self._runner.start(step.argv)
            return ModelRunnerResult(exit_code=0, stdout="", stderr="")
        return self._runner.run(step.argv, timeout_seconds=step.timeout_seconds)


def _base_url(value: str) -> str | None:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    port = f":{parsed.port}" if parsed.port is not None else ""
    return f"{parsed.scheme}://{host}{port}"


def _probe_output(result: ModelRunnerResult) -> tuple[int | None, str | None]:
    fields = result.stdout.strip().split(maxsplit=1)
    if len(fields) != 2 or not re.fullmatch(r"[1-5]\d\d", fields[0]):
        return None, None
    return int(fields[0]), _base_url(fields[1])


def _require_success(result: ModelRunnerResult, kind: EnumWalkStepKind) -> None:
    if result.exit_code:
        raise RuntimeError(
            f"{kind}: exit {result.exit_code}: {result.stderr or result.stdout}"
        )


class HandlerOnboardingCleanVmWalk:
    """Synchronous operator effect; operational failures return a cleanup receipt."""

    def __init__(
        self,
        runner: ProtocolTartRunner | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._runner = runner
        self._clock = clock

    def handle(self, request: ModelWalkRequest) -> ModelWalkReceipt:
        started = self._clock()
        started_at = datetime.now(UTC).isoformat()
        steps: list[ModelWalkStepRecord] = []
        refused: list[str] = []
        failures: list[str] = []
        log = ""
        # The walk owns the VM only when its clone succeeded or its reply was lost.
        # A clone Tart refused (the name already exists) or the step guard refused
        # before it started created nothing, so cleanup must leave that VM alone.
        vm_owned = False
        clone_confirmed = False
        clone_refused = False
        deleted = False
        dashboard_started = False
        dashboard = ModelDashboardResult(
            failure_text="not attempted: onboarding did not complete"
        )
        vm_spec = ModelVmSpec(
            image=request.image or "unresolved",
            cpu=request.cpu or 0,
            memory_gb=request.memory_gb or 0,
            vm_name=request.vm_name or "unresolved",
        )
        guard: DocumentedStepGuard | None = None

        def issue(kind: EnumWalkStepKind) -> ModelRunnerResult:
            if guard is None:
                raise RuntimeError("documented step guard is unavailable")
            step = ModelWalkStep(
                kind=kind,
                argv=build_step_argv(kind, request),
                timeout_seconds=float(request.step_timeout_seconds),
            )
            before = self._clock()
            try:
                result = guard.issue(step)
            except StepNotDocumentedError:
                refused.append(str(kind))
                raise
            except _OPERATION_ERRORS:
                steps.append(
                    ModelWalkStepRecord(
                        kind=kind,
                        argv=step.argv,
                        exit_code=125,
                        seconds=self._clock() - before,
                    )
                )
                raise
            steps.append(
                ModelWalkStepRecord(
                    kind=kind,
                    argv=step.argv,
                    exit_code=result.exit_code,
                    seconds=self._clock() - before,
                )
            )
            return result

        def run(kind: EnumWalkStepKind) -> ModelRunnerResult:
            result = issue(kind)
            _require_success(result, kind)
            return result

        try:
            request = resolve_request(request)
            require_guest_login(request)
            vm_spec = ModelVmSpec.model_validate(
                {
                    "image": request.image,
                    "cpu": request.cpu,
                    "memory_gb": request.memory_gb,
                    "vm_name": request.vm_name,
                }
            )
            runner = self._runner or HandlerSshTart(request.tart_host)
            guard = DocumentedStepGuard(request, runner)
            assert request.tart_path is not None
            version = runner.run(
                (request.tart_path, "--version"),
                timeout_seconds=float(request.step_timeout_seconds),
            )
            if version.exit_code:
                raise RuntimeError(
                    f"tart not found at {request.tart_path} on {request.tart_host}"
                )
            try:
                clone = issue(EnumWalkStepKind.TART_CLONE)
            except StepNotDocumentedError:
                raise
            except _OPERATION_ERRORS:
                # The command reached the transport and no reply came back.
                vm_owned = True
                raise
            if clone.exit_code == 0:
                vm_owned = clone_confirmed = True
            elif _clone_reply_lost(clone):
                vm_owned = True
            else:
                clone_refused = True
            _require_success(clone, EnumWalkStepKind.TART_CLONE)
            run(EnumWalkStepKind.TART_SET_SPEC)
            run(EnumWalkStepKind.TART_RUN)
            request = bind_guest_ip(request, run(EnumWalkStepKind.TART_IP).stdout)
            guard = DocumentedStepGuard(request, runner)
            for attempt in range(20):
                fetched = issue(EnumWalkStepKind.GUEST_FETCH_SCRIPT)
                if fetched.exit_code == 0:
                    break
                output = (fetched.stdout + fetched.stderr).lower()
                if attempt == 19 or not any(
                    word in output for word in ("agent", "connect")
                ):
                    _require_success(fetched, EnumWalkStepKind.GUEST_FETCH_SCRIPT)
                time.sleep(6)
            checksum = issue(EnumWalkStepKind.GUEST_VERIFY_CHECKSUM)
            if checksum.exit_code:
                raise RuntimeError("checksum mismatch")
            script = issue(EnumWalkStepKind.GUEST_RUN_SCRIPT)
            log = script.stdout
            readback = issue(EnumWalkStepKind.GUEST_READ_LOG)
            if readback.exit_code == 0:
                log = readback.stdout
            else:
                _require_success(readback, EnumWalkStepKind.GUEST_READ_LOG)
            phases = parse_phases(log)
            failed_phases = [p for p in phases if p.result == EnumPhaseResult.FAILED]
            if failed_phases:
                raise RuntimeError(
                    "\n".join(p.failure_text or p.name for p in failed_phases)
                )
            _require_success(script, EnumWalkStepKind.GUEST_RUN_SCRIPT)
            if parse_delegated_run(log).status != "success":
                raise RuntimeError("no successful delegated run in onboarding log")
            dashboard = ModelDashboardResult(failure_text="dashboard did not complete")
            run(EnumWalkStepKind.GUEST_DASHBOARD_START)
            dashboard_started = True
            root = issue(EnumWalkStepKind.GUEST_DASHBOARD_PROBE_ROOT)
            root_status, base = _probe_output(root)
            dashboard = ModelDashboardResult(
                root_http_status=root_status, base_url=base
            )
            _require_success(root, EnumWalkStepKind.GUEST_DASHBOARD_PROBE_ROOT)
            projections = issue(EnumWalkStepKind.GUEST_DASHBOARD_PROBE_PROJECTIONS)
            projection_status, _ = _probe_output(projections)
            dashboard = dashboard.model_copy(
                update={"projections_http_status": projection_status}
            )
            _require_success(
                projections, EnumWalkStepKind.GUEST_DASHBOARD_PROBE_PROJECTIONS
            )
            if root_status != 200 or projection_status != 200 or base is None:
                raise RuntimeError(
                    f"dashboard probe failed: root={root_status}, projections={projection_status}"
                )
        except _OPERATION_ERRORS as exc:
            failures.append(str(exc))
            if dashboard.failure_text and dashboard.failure_text.startswith(
                "not attempted:"
            ):
                dashboard = dashboard.model_copy(
                    update={"failure_text": f"not attempted: {exc}"}
                )
            else:
                dashboard = dashboard.model_copy(update={"failure_text": str(exc)})
        finally:
            cleanup = []
            if dashboard_started:
                cleanup.append(EnumWalkStepKind.GUEST_DASHBOARD_STOP)
            if vm_owned:
                cleanup.extend(
                    (EnumWalkStepKind.TART_STOP, EnumWalkStepKind.TART_DELETE)
                )
            for kind in cleanup:
                try:
                    result = issue(kind)
                    # An unconfirmed clone that left no VM behind is clean, not a failure.
                    absent = (
                        result.exit_code != 0
                        and not clone_confirmed
                        and kind != EnumWalkStepKind.GUEST_DASHBOARD_STOP
                        and _VM_ABSENT.search(result.stderr + result.stdout) is not None
                    )
                    if not absent:
                        _require_success(result, kind)
                    if kind == EnumWalkStepKind.TART_DELETE:
                        deleted = True
                except _OPERATION_ERRORS as exc:
                    failures.append(str(exc))
            if vm_owned and not deleted:
                failures.append(
                    f"VM {request.vm_name} left undeleted on {request.tart_host}"
                )
            if clone_refused:
                failures.append(
                    f"VM {request.vm_name} was not created by this walk and was "
                    f"left untouched on {request.tart_host}"
                )

        # Redact the whole log before parsing and before the tail is cut, so a secret
        # straddling the cut cannot survive as an unrecognisable fragment.
        log = redact(log)
        parsed_phases: tuple[ModelPhaseRecord, ...] = ()
        pins: tuple[ModelInstalledPin, ...] = ()
        delegated = ModelDelegatedRun()
        first_delegated_seconds: float | None = None
        macos_version: str | None = None
        try:
            parsed_phases = parse_phases(log)
            pins = parse_pins(log)
            delegated = parse_delegated_run(log)
            first_delegated_seconds = seconds_to_first_delegated_run(log)
            macos_version = parse_macos_version(log)
        except _OPERATION_ERRORS as exc:
            failures.append(f"could not parse onboarding log: {exc}")
        # The receipt is published: one sanitiser over every string field of it.
        return sanitize_receipt(
            ModelWalkReceipt(
                outcome=EnumWalkOutcome.FAILED if failures else EnumWalkOutcome.PASSED,
                script_sha=request.script_sha,
                checksum_line=request.checksum_line,
                providers=request.providers,
                local_url=request.local_url,
                vm_spec=vm_spec.model_copy(update={"macos_version": macos_version}),
                phases=parsed_phases,
                steps=tuple(steps),
                total_seconds=self._clock() - started,
                seconds_to_first_delegated_run=first_delegated_seconds,
                delegated_run=delegated,
                dashboard=dashboard,
                pins_installed=pins,
                omnibase_spi_version=next(
                    (p.version for p in pins if p.name == "omnibase_spi"), None
                ),
                branches_installed=request.branches_installed,
                vm_deleted=deleted,
                failure_text="\n".join(failures) if failures else None,
                terminal_failure_cause="\n".join(failures) if failures else None,
                log_tail=log[-4000:],
                hand_fix_steps=tuple(refused),
                started_at=started_at,
                finished_at=datetime.now(UTC).isoformat(),
            )
        )
