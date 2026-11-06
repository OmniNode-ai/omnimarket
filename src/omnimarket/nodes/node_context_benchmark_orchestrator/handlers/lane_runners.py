# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Lane runners for the context benchmark (OMN-21896).

Each runner asks one lane to do one task in one arm's copy and returns the tokens
it reports. A token count the lane does not report is ``UNKNOWN``, never 0. No
runner holds a credential: the headless CLIs use their own login, the delegation
route resolves keys inside the runtime, and the Jev arm goes through
node_typed_decision_effect, which resolves ``llm.typesafe.api_key`` from the
delegation credential store at its own boundary.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from omnimarket.nodes.node_context_benchmark_orchestrator.models.model_context_benchmark import (
    UNKNOWN,
    ModelBenchmarkLaneSpec,
    ModelBenchmarkTask,
    ModelLaneInvocation,
)

JEV_SECRET_REF = "llm.typesafe.api_key"
JEV_BACKEND_ID = "cloud-typesafe-jev"
_TYPED_DECISION_MODULE = "omnimarket.nodes.node_typed_decision_effect"
_SECRETISH_ENV = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|PYTHONPATH", re.I)
_REDACTIONS = (
    re.compile(r"(?i)(bearer|authorization:?)\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"\b(sk|key|tok|glpat|ghp|xox[bp])[-_][A-Za-z0-9_-]{12,}"),
)
_DETAIL_LIMIT = 300


def scrub(text: str) -> str:
    """Drop anything shaped like a bearer token or API key from a detail string."""
    for pattern in _REDACTIONS:
        text = pattern.sub("[redacted]", text)
    return text[:_DETAIL_LIMIT]


def sanitized_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """The process environment without anything that looks like a credential."""
    source = os.environ if base is None else base
    return {k: v for k, v in source.items() if not _SECRETISH_ENV.search(k)}


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False


CommandFn = Callable[
    [Sequence[str], Path | None, float, str | None, Mapping[str, str] | None],
    CommandResult,
]


def run_command(
    argv: Sequence[str],
    cwd: Path | None,
    timeout: float,
    stdin: str | None,
    env: Mapping[str, str] | None,
) -> CommandResult:
    try:
        done = subprocess.run(
            list(argv),
            cwd=cwd,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=None if env is None else dict(env),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return CommandResult(124, _text(exc.stdout), _text(exc.stderr), True)
    except OSError as exc:
        return CommandResult(127, "", f"{type(exc).__name__}: {exc}")
    return CommandResult(done.returncode, done.stdout, done.stderr)


def _text(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value if isinstance(value, str) else ""


def _int_or_unknown(value: object) -> int | str:
    return value if isinstance(value, int) and not isinstance(value, bool) else UNKNOWN


class ProtocolBenchmarkLane(Protocol):
    def invoke(
        self,
        *,
        spec: ModelBenchmarkLaneSpec,
        task: ModelBenchmarkTask,
        workdir: Path,
        context_text: str | None,
        timeout: float,
    ) -> ModelLaneInvocation: ...


class LaneClaudeHeadless:
    """``claude -p`` in the arm's copy; usage comes from its JSON result."""

    def __init__(self, command_fn: CommandFn = run_command) -> None:
        self._run = command_fn

    def invoke(
        self,
        *,
        spec: ModelBenchmarkLaneSpec,
        task: ModelBenchmarkTask,
        workdir: Path,
        context_text: str | None,
        timeout: float,
    ) -> ModelLaneInvocation:
        argv = (
            "claude",
            "-p",
            "--output-format",
            "json",
            "--permission-mode",
            "acceptEdits",
            "--allowedTools",
            "Read Edit Write Glob Grep Bash",
        )
        done = self._run(argv, workdir, timeout, task.prompt, None)
        return parse_claude_output(done)


def parse_claude_output(done: CommandResult) -> ModelLaneInvocation:
    if done.timed_out:
        return ModelLaneInvocation(error="timeout")
    try:
        payload = json.loads(done.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return ModelLaneInvocation(
            error=scrub(f"unparseable output rc={done.returncode}")
        )
    if not isinstance(payload, dict):
        return ModelLaneInvocation(error="unparseable output")
    if payload.get("is_error"):
        return ModelLaneInvocation(
            error=scrub(str(payload.get("result", "claude error")))
        )
    raw_usage = payload.get("usage")
    usage: dict[str, object] = raw_usage if isinstance(raw_usage, dict) else {}
    models = payload.get("modelUsage")
    served = ",".join(sorted(models)) if isinstance(models, dict) and models else None
    return ModelLaneInvocation(
        output_text=str(payload.get("result", "")),
        input_tokens=_int_or_unknown(usage.get("input_tokens")),
        output_tokens=_int_or_unknown(usage.get("output_tokens")),
        cache_read_tokens=_int_or_unknown(usage.get("cache_read_input_tokens")),
        cache_write_tokens=_int_or_unknown(usage.get("cache_creation_input_tokens")),
        served_model=served,
    )


class LaneCodexHeadless:
    """``codex exec`` at high effort, no model flag; usage from turn.completed events."""

    def __init__(self, command_fn: CommandFn = run_command) -> None:
        self._run = command_fn

    def invoke(
        self,
        *,
        spec: ModelBenchmarkLaneSpec,
        task: ModelBenchmarkTask,
        workdir: Path,
        context_text: str | None,
        timeout: float,
    ) -> ModelLaneInvocation:
        argv = (
            "codex",
            "exec",
            "-C",
            str(workdir),
            "--skip-git-repo-check",
            "-c",
            'model_reasoning_effort="high"',
            "-s",
            "workspace-write",
            "--json",
            "-",
        )
        return parse_codex_output(self._run(argv, workdir, timeout, task.prompt, None))


def parse_codex_output(done: CommandResult) -> ModelLaneInvocation:
    if done.timed_out:
        return ModelLaneInvocation(error="timeout")
    totals: dict[str, int] = {}
    seen = False
    text = ""
    for line in done.stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        item = event.get("item")
        if isinstance(item, dict) and item.get("type") == "agent_message":
            text = str(item.get("text", ""))
        usage = event.get("usage")
        if event.get("type") == "turn.completed" and isinstance(usage, dict):
            seen = True
            for key in (
                "input_tokens",
                "cached_input_tokens",
                "cache_write_input_tokens",
                "output_tokens",
            ):
                value = usage.get(key)
                if isinstance(value, int):
                    totals[key] = totals.get(key, 0) + value
    if done.returncode != 0:
        return ModelLaneInvocation(error=scrub(f"codex rc={done.returncode}"))
    if not seen:
        return ModelLaneInvocation(
            output_text=text, error="no turn.completed usage event"
        )
    cached = totals.get("cached_input_tokens", 0)
    # Codex reports input_tokens inclusive of the cached part; report the fresh part.
    return ModelLaneInvocation(
        output_text=text,
        input_tokens=max(totals.get("input_tokens", 0) - cached, 0),
        output_tokens=totals.get("output_tokens", 0),
        cache_read_tokens=cached,
        cache_write_tokens=totals.get("cache_write_input_tokens", 0),
        served_model="codex-config-default",
    )


_RUN_ID = re.compile(
    r"/runs/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
)
_FILE_BLOCK = re.compile(r"^### (\S+)\n```[A-Za-z0-9_+-]*\n(.*?)\n```", re.S | re.M)


def route_prompt(
    task: ModelBenchmarkTask, workdir: Path, context_text: str | None
) -> str:
    shown = "\n\n".join(
        f"### {rel}\n```\n{(workdir / rel).read_text(encoding='utf-8')}\n```"
        for rel in task.edit_files
    )
    guidance = (
        f"Repository guidance:\n---\n{context_text}\n---\n\n" if context_text else ""
    )
    return (
        f"{guidance}Task for a small Python repository: {task.prompt}\n\n"
        f"Current files:\n\n{shown}\n\n"
        "Reply with the complete new content of every file you change, each as a "
        "heading line `### <path>` followed by one fenced code block, and nothing else."
    )


def apply_file_blocks(output: str, task: ModelBenchmarkTask, workdir: Path) -> int:
    """Write the returned files; only ``edit_files`` may be written."""
    written = 0
    for rel, body in _FILE_BLOCK.findall(output):
        if rel in task.edit_files:
            (workdir / rel).write_text(body + "\n", encoding="utf-8")
            written += 1
    return written


class LaneDelegateRoute:
    """A text-only model reached through ``onex delegate``; tokens from its run record."""

    def __init__(
        self,
        *,
        onex_bin: str,
        state_root: Path,
        lane: str,
        ticket: str,
        transport_flags: tuple[str, ...] = (
            "--bus",
            "kafka",
            "--lane",
            "dev",
            "--locus",
            "deployed-lane",
        ),
        command_fn: CommandFn = run_command,
    ) -> None:
        self._onex = onex_bin
        self._state_root = state_root
        self._lane = lane
        self._ticket = ticket
        self._flags = transport_flags
        self._run = command_fn

    def invoke(
        self,
        *,
        spec: ModelBenchmarkLaneSpec,
        task: ModelBenchmarkTask,
        workdir: Path,
        context_text: str | None,
        timeout: float,
    ) -> ModelLaneInvocation:
        if not task.edit_files:
            return ModelLaneInvocation(
                not_applicable="task names no edit_files a text-only model can return"
            )
        prompt = route_prompt(task, workdir, context_text)
        argv = [
            self._onex,
            "delegate",
            prompt,
            "--task-type",
            "code_generation",
            "--ticket",
            self._ticket,
            "--caller-lane",
            self._lane,
            *self._flags,
            "--state-root",
            str(self._state_root),
        ]
        if spec.backend_id:
            argv += ["--backend-id", spec.backend_id]
        done = self._run(argv, workdir, timeout, None, None)
        if done.timed_out:
            return ModelLaneInvocation(error="timeout")
        match = _RUN_ID.search(done.stdout + "\n" + done.stderr)
        if match is None:
            return ModelLaneInvocation(error=scrub("no run id in delegate output"))
        return read_route_run(
            self._state_root / "runs" / match.group(1), done.returncode
        )


def read_route_run(run_dir: Path, returncode: int) -> ModelLaneInvocation:
    result_file, flow_file = run_dir / "result.txt", run_dir / "workflow_result.json"
    if returncode != 0 or not result_file.is_file():
        return ModelLaneInvocation(error=f"delegate rc={returncode} without a result")
    output = result_file.read_text(encoding="utf-8")
    metrics: Mapping[str, object] = {}
    served: str | None = None
    if flow_file.is_file():
        try:
            payload = json.loads(flow_file.read_text(encoding="utf-8"))
            body = payload["terminal_payload"]["payload"]
            metrics = body.get("metrics") or {}
            served = body.get("model_name")
        except (ValueError, KeyError, TypeError, AttributeError):
            metrics = {}
    return ModelLaneInvocation(
        output_text=output,
        input_tokens=_int_or_unknown(metrics.get("input_tokens")),
        output_tokens=_int_or_unknown(metrics.get("output_tokens")),
        # The delegation route reports no cache split.
        served_model=served if isinstance(served, str) else None,
    )


class LaneJevTypedDecision:
    """Typed-decision tasks only, through node_typed_decision_effect's CLI.

    The key is ``llm.typesafe.api_key`` in the delegation credential store, resolved
    inside that node. This runner never reads, passes or logs it, and starts the
    node with an environment stripped of anything credential-shaped.
    """

    def __init__(
        self,
        *,
        repository: str = "OmniNode-ai/omnimarket",
        command_fn: CommandFn = run_command,
    ) -> None:
        self._repository = repository
        self._run = command_fn

    def invoke(
        self,
        *,
        spec: ModelBenchmarkLaneSpec,
        task: ModelBenchmarkTask,
        workdir: Path,
        context_text: str | None,
        timeout: float,
    ) -> ModelLaneInvocation:
        question = task.jev_question
        if question is None:
            return ModelLaneInvocation(
                not_applicable=(
                    "Jev answers typed decisions over a fixed answer set; this is an "
                    "agent task that edits files and runs a check, which it cannot do"
                )
            )
        state: dict[str, object] = {"task": task.task_id}
        if context_text:
            state["repository_guidance"] = context_text
        request = {
            "correlation_id": _correlation_id(task.task_id, bool(context_text)),
            "work_unit_repository": self._repository,
            "state": state,
            "kind": "choice",
            "instructions": question.instructions,
            "criteria": {k: {"what": v} for k, v in question.options.items()},
        }
        argv = (sys.executable, "-m", _TYPED_DECISION_MODULE)
        done = self._run(argv, workdir, timeout, json.dumps(request), sanitized_env())
        if done.timed_out:
            return ModelLaneInvocation(error="timeout")
        try:
            result = json.loads(done.stdout)
        except ValueError:
            return ModelLaneInvocation(
                error=scrub(f"unparseable decision output rc={done.returncode}")
            )
        decided_by = str(result.get("decided_by", ""))
        if decided_by != "model":
            reason = str(result.get("reason", ""))
            if reason == "credential_not_registered":
                return ModelLaneInvocation(blocked="credential-absent")
            return ModelLaneInvocation(
                error=scrub(f"decided_by={decided_by} reason={reason}")
            )
        return ModelLaneInvocation(
            output_text=str(result.get("model_answer", "")),
            input_tokens=_int_or_unknown(result.get("input_tokens")),
            output_tokens=_int_or_unknown(result.get("output_tokens")),
            served_model=result.get("served_model")
            if isinstance(result.get("served_model"), str)
            else None,
            check_passed=result.get("model_answer") == question.expected_answer,
        )


def _correlation_id(task_id: str, with_context: bool) -> str:
    import uuid

    return str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"context-benchmark/{task_id}/{with_context}")
    )
