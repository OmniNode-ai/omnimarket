# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerStackCompose: executes ONE step of a stability stack install plan (OMN-23968).

``handle(ModelStackStepRequest) -> ModelStackStepResult``. The overlays, the command runner, the health prober and the
environment are constructor parameters, so the node runs the same on a fake runner and on the live adapters. A request for
a stack the overlay does not declare, or for a stack that lives on another host, is refused before any command is run.
A failed command or reading is a ``failed`` result, never an exception.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from omnimarket.nodes.node_stability_stack_compose_effect.handlers.stack_commands import (
    compose_argv,
    compose_env,
    consume_argv,
    digest_files,
    gone_argvs,
    parse_ps,
    produce_argv,
    rendered_paths,
)
from omnimarket.nodes.node_stability_stack_compose_effect.models.model_stack_compose import (
    EnumStackRefusalReason,
    EnumStackStepOutcome,
    ModelCommandOutput,
    ModelStackStepRequest,
    ModelStackStepResult,
)
from omnimarket.nodes.node_stability_stack_compose_effect.overlay import (
    ModelStackComposeOverlay,
)
from omnimarket.nodes.node_stability_stack_compose_effect.protocols.protocol_stack_command_runner import (
    ProtocolStackCommandRunner,
)
from omnimarket.nodes.node_stability_stack_compose_effect.protocols.protocol_stack_health_prober import (
    ProtocolStackHealthProber,
)
from omnimarket.nodes.node_stability_stack_plan_compute.models.model_stack_plan import (
    EnumStackStepKind,
    ModelStackDescription,
    ModelStackPlanOverlay,
)

_OK = EnumStackStepOutcome.OK
_FAILED = EnumStackStepOutcome.FAILED
_REFUSED = EnumStackStepOutcome.REFUSED
_TAIL_CHARS = 400


class HandlerStackCompose:
    """One plan step, run through the injected runner and prober."""

    def __init__(
        self,
        plan_overlay: ModelStackPlanOverlay,
        overlay: ModelStackComposeOverlay,
        runner: ProtocolStackCommandRunner,
        prober: ProtocolStackHealthProber,
        environ: Mapping[str, str],
    ) -> None:
        self._plan_overlay = plan_overlay
        self._overlay = overlay
        self._runner = runner
        self._prober = prober
        self._environ = environ

    def handle(self, request: ModelStackStepRequest) -> ModelStackStepResult:
        kind = request.step.kind
        stacks = {s.name: s for s in self._plan_overlay.stacks}
        stack = stacks.get(request.stack)
        if stack is None:
            return self._refuse(
                request, EnumStackRefusalReason.UNKNOWN_STACK, request.stack
            )
        if stack.host != self._overlay.local_host:
            return self._refuse(
                request, EnumStackRefusalReason.HOST_NOT_LOCAL, stack.host
            )
        if kind == EnumStackStepKind.RENDER:
            return self._render(request, stack)
        try:
            files = rendered_paths(self._overlay, stack, self._environ)
        except KeyError as exc:
            return self._refuse(request, EnumStackRefusalReason.NOT_RENDERED, str(exc))
        if not all(path.is_file() for path in files):
            return self._refuse(
                request, EnumStackRefusalReason.NOT_RENDERED, str(files[0].parent)
            )
        digest = digest_files(files)
        try:
            result = self._run_step(request, stack, files)
        except Exception as exc:
            return self._result(
                request, _FAILED, reading=f"{type(exc).__name__}: {exc}", digest=digest
            )
        if result is None:
            return self._refuse(request, EnumStackRefusalReason.UNKNOWN_STEP, str(kind))
        return result.model_copy(update={"digest": digest})

    # -- steps ----------------------------------------------------------------------------------

    def _run_step(
        self,
        request: ModelStackStepRequest,
        stack: ModelStackDescription,
        files: tuple[Path, ...],
    ) -> ModelStackStepResult | None:
        kind = request.step.kind
        timeout = self._overlay.command_timeout_seconds
        env = compose_env(self._environ, stack)
        if kind == EnumStackStepKind.SNAPSHOT:
            out = self._runner.run(
                compose_argv(self._overlay, stack, files, "ps", "--format", "json"),
                env,
                timeout,
            )
            if out.returncode != 0:
                return self._result(request, _FAILED, reading=_tail(out))
            return self._result(
                request,
                _OK,
                reading=f"{len(parse_ps(out.stdout))} containers in the project",
            )
        if kind == EnumStackStepKind.UP:
            services = tuple(request.step.target.split())
            verb = (
                "up",
                "-d",
                "--wait",
                "--wait-timeout",
                str(self._overlay.up_wait_seconds),
                *services,
            )
            out = self._runner.run(
                compose_argv(self._overlay, stack, files, *verb), env, timeout
            )
            return self._from_command(request, out, f"up {' '.join(services)}")
        if kind == EnumStackStepKind.READ_SERVICES:
            return self._read_services(request, stack, files, env)
        if kind == EnumStackStepKind.READ_HEALTH:
            return self._read_health(request, stack)
        if kind == EnumStackStepKind.EVENT_ROUNDTRIP:
            return self._event_roundtrip(request, stack, env)
        if kind == EnumStackStepKind.DOWN:
            verb = (
                "down",
                "--remove-orphans",
                *(("-v",) if stack.remove_volumes_on_down else ()),
            )
            out = self._runner.run(
                compose_argv(self._overlay, stack, files, *verb), env, timeout
            )
            return self._from_command(request, out, "down")
        if kind == EnumStackStepKind.READ_GONE:
            leftovers: list[str] = []
            for argv in gone_argvs(self._overlay, stack):
                out = self._runner.run(argv, env, timeout)
                leftovers.extend(out.stdout.split())
            if leftovers:
                return self._result(
                    request,
                    _FAILED,
                    reading=f"left behind: {' '.join(leftovers)}",
                    items=tuple(leftovers),
                )
            return self._result(
                request, _OK, reading="no container or volume of the project remains"
            )
        return None

    def _render(
        self, request: ModelStackStepRequest, stack: ModelStackDescription
    ) -> ModelStackStepResult:
        source_dir = self._environ.get(stack.source_dir_env, "")
        render_dir = self._environ.get(self._overlay.render_dir_env, "")
        if not source_dir:
            return self._refuse(
                request, EnumStackRefusalReason.SOURCE_DIR_UNSET, stack.source_dir_env
            )
        if not render_dir:
            return self._refuse(
                request,
                EnumStackRefusalReason.NOT_RENDERED,
                self._overlay.render_dir_env,
            )
        texts: list[str] = []
        for name in stack.compose_files:
            text = (Path(source_dir) / name).read_text(encoding="utf-8")
            for old, new in stack.name_rewrites:
                text = text.replace(old, new)
            texts.append(text)
        joined = "\n".join(texts)
        for declared in (*stack.container_names, *stack.volumes):
            if declared not in joined:
                return self._refuse(
                    request, EnumStackRefusalReason.DECLARED_NAME_ABSENT, declared
                )
        written: list[str] = []
        for path, text in zip(
            rendered_paths(self._overlay, stack, self._environ), texts, strict=True
        ):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            written.append(path.name)
        digest = digest_files(rendered_paths(self._overlay, stack, self._environ))
        return self._result(
            request,
            _OK,
            reading=f"rendered {len(written)} files",
            digest=digest,
            items=tuple(written),
        )

    def _read_services(
        self,
        request: ModelStackStepRequest,
        stack: ModelStackDescription,
        files: tuple[Path, ...],
        env: Mapping[str, str],
    ) -> ModelStackStepResult:
        out = self._runner.run(
            compose_argv(self._overlay, stack, files, "ps", "-a", "--format", "json"),
            env,
            self._overlay.command_timeout_seconds,
        )
        if out.returncode != 0:
            return self._result(request, _FAILED, reading=_tail(out))
        listed = {str(row.get("Name", "")): row for row in parse_ps(out.stdout)}
        missing = [name for name in stack.container_names if name not in listed]
        if missing:
            return self._result(
                request, _FAILED, reading=f"not listed: {' '.join(missing)}"
            )
        for name in stack.container_names:
            row = listed[name]
            state, health, code = (
                str(row.get("State", "")),
                str(row.get("Health", "")),
                row.get("ExitCode", 0),
            )
            if state == "running" and health in ("", "healthy"):
                continue
            if state == "exited" and code == 0:
                continue
            return self._result(
                request,
                _FAILED,
                reading=f"{name}: state={state} health={health or 'none'} exit={code}",
            )
        return self._result(
            request,
            _OK,
            reading=f"{len(stack.container_names)} containers healthy",
            items=stack.container_names,
        )

    def _read_health(
        self, request: ModelStackStepRequest, stack: ModelStackDescription
    ) -> ModelStackStepResult:
        reads = [r for r in stack.health_reads if r.container == request.step.target]
        if not reads:
            return self._refuse(
                request, EnumStackRefusalReason.UNKNOWN_STEP, request.step.target
            )
        read = reads[0]
        port = dict(stack.ports)[read.port_key]
        url = f"http://127.0.0.1:{port}/{read.path.lstrip('/')}"
        status = self._prober.status(url, self._overlay.health_timeout_seconds)
        outcome = _OK if status == read.expect_status else _FAILED
        return self._result(
            request,
            outcome,
            reading=f"{url} answered {status}, expected {read.expect_status}",
        )

    def _event_roundtrip(
        self,
        request: ModelStackStepRequest,
        stack: ModelStackDescription,
        env: Mapping[str, str],
    ) -> ModelStackStepResult:
        message = f"{stack.event_roundtrip.message}-{request.correlation_id}"
        produced = self._runner.run(
            produce_argv(self._overlay, stack, message),
            env,
            self._overlay.command_timeout_seconds,
        )
        if produced.returncode != 0:
            return self._result(request, _FAILED, reading=f"produce: {_tail(produced)}")
        consumed = self._runner.run(
            consume_argv(self._overlay, stack), env, self._overlay.event_timeout_seconds
        )
        if consumed.returncode != 0 or message not in consumed.stdout:
            return self._result(
                request, _FAILED, reading=f"consumed 0 of 1: {_tail(consumed)}"
            )
        return self._result(
            request, _OK, reading="published 1 and consumed 1", items=(message,)
        )

    # -- results --------------------------------------------------------------------------------

    def _from_command(
        self, request: ModelStackStepRequest, out: ModelCommandOutput, what: str
    ) -> ModelStackStepResult:
        if out.returncode != 0:
            return self._result(
                request,
                _FAILED,
                reading=f"{what} exited {out.returncode}: {_tail(out)}",
            )
        return self._result(request, _OK, reading=f"{what} exited 0")

    def _refuse(
        self,
        request: ModelStackStepRequest,
        reason: EnumStackRefusalReason,
        reading: str,
    ) -> ModelStackStepResult:
        return self._result(request, _REFUSED, reason=reason, reading=reading)

    def _result(
        self,
        request: ModelStackStepRequest,
        outcome: EnumStackStepOutcome,
        *,
        reason: EnumStackRefusalReason | str = "",
        reading: str = "",
        digest: str = "",
        items: tuple[str, ...] = (),
    ) -> ModelStackStepResult:
        return ModelStackStepResult(
            correlation_id=request.correlation_id,
            stack=request.stack,
            step_kind=str(request.step.kind),
            outcome=outcome,
            reason=reason,
            reading=reading,
            digest=digest,
            items=items,
        )


def _tail(out: ModelCommandOutput) -> str:
    return (out.stderr or out.stdout)[-_TAIL_CHARS:]


__all__ = ["HandlerStackCompose"]
