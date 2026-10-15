# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run one attempt per rung and case, preserving failure rows."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import Protocol

from omnimarket.delegation.rung_eval.graders import grade_classification, grade_grouping
from omnimarket.delegation.rung_eval.models import (
    EnumRungEvalStatus,
    ModelRungEvalCase,
    ModelRungEvalRow,
    ModelRungSpec,
    ModelTransportResult,
)
from omnimarket.tenant_credential_ref import is_tenant_credential_ref


class RungUnresolvedError(Exception):
    """The rung's endpoint or model environment variable is unset."""


class Transport(Protocol):
    def complete(self, rung: ModelRungSpec, prompt: str) -> ModelTransportResult: ...


def run_night(
    night: date,
    rungs: list[ModelRungSpec],
    cases: list[ModelRungEvalCase],
    transport: Transport,
) -> list[ModelRungEvalRow]:
    rows: list[ModelRungEvalRow] = []
    for rung in rungs:
        for case in cases:
            result: ModelTransportResult | None = None
            score = 0.0
            passed = False
            try:
                result = transport.complete(rung, case.prompt)
                if case.answer_key is not None:
                    grade = grade_classification(result.text, case.answer_key)
                elif case.ticket_ids is not None and case.max_groups is not None:
                    grade = grade_grouping(
                        result.text, case.ticket_ids, case.known_ids, case.max_groups
                    )
                else:
                    raise ValueError(f"No grading specification for {case.case_id}")
                score, passed, detail = grade.score, grade.passed, grade.detail
                status = (
                    EnumRungEvalStatus.PASSED if passed else EnumRungEvalStatus.FAILED
                )
            except RungUnresolvedError as exc:
                status = EnumRungEvalStatus.UNRESOLVED
                detail = str(exc)
            except Exception as exc:
                status = EnumRungEvalStatus.ERROR
                detail = f"{type(exc).__name__}: {exc}"
            rows.append(
                ModelRungEvalRow(
                    night=night,
                    rung_id=rung.rung_id,
                    task_class=case.task_class,
                    case_id=case.case_id,
                    status=status,
                    score=score,
                    passed=passed,
                    latency_ms=result.latency_ms if result is not None else None,
                    model_id=result.model_id if result is not None else None,
                    detail=detail,
                    tenant_id=result.tenant_id if result is not None else None,
                    credential_ref=(
                        result.credential_ref if result is not None else None
                    ),
                )
            )
    return rows


def _rung_env(name: str) -> str:
    """Read one environment variable whose name a rung spec declares."""
    return os.environ[name]  # ONEX_FLAG_EXEMPT: name declared by the rung spec


class OpenAIChatTransport:
    """Single-attempt chat completions over urllib."""

    def complete(self, rung: ModelRungSpec, prompt: str) -> ModelTransportResult:
        if rung.base_url_env is None or rung.model_env is None:
            raise ValueError(f"{rung.rung_id}: no endpoint declared")
        try:
            base_url = _rung_env(rung.base_url_env)
            model = _rung_env(rung.model_env)
            api_key = (
                _rung_env(rung.api_key_env) if rung.api_key_env is not None else None
            )
        except KeyError as exc:
            raise RungUnresolvedError(
                f"{rung.rung_id}: environment variable {exc.args[0]} is unset"
            ) from exc
        headers = {"Content-Type": "application/json"}
        if api_key is not None:
            headers["Authorization"] = f"Bearer {api_key}"
        payload = json.dumps(
            {
                "model": model,
                "temperature": 0,
                "messages": [{"role": "user", "content": prompt}],
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{base_url.rstrip('/')}/chat/completions",
            data=payload,
            headers=headers,
            method="POST",
        )
        started = time.monotonic()
        with urllib.request.urlopen(request, timeout=600) as response:
            body = json.load(response)
        return ModelTransportResult(
            text=body["choices"][0]["message"]["content"],
            model_id=body["model"],
            latency_ms=int((time.monotonic() - started) * 1000),
        )


#: (argv, stdin, environment) -> (exit code, stdout, stderr) of one ``onex delegate``.
DelegateRun = Callable[[Sequence[str], str, Mapping[str, str]], tuple[int, str, str]]

_DELEGATE_TIMEOUT_S = 600
#: The eval grades the answer itself, so the delegation's task class only has to
#: be one the quality gate accepts for free-form analysis.
_DELEGATE_TASK_TYPE = "reasoning"
_TENANT_ENV = "ONEX_TENANT_ID"


def _onex_executable() -> str:
    beside = Path(sys.executable).with_name("onex")
    if beside.is_file():
        return str(beside)
    return shutil.which("onex") or "onex"


def _run_subprocess(
    argv: Sequence[str], stdin: str, env: Mapping[str, str]
) -> tuple[int, str, str]:
    done = subprocess.run(
        list(argv),
        input=stdin,
        capture_output=True,
        text=True,
        env=dict(env),
        timeout=_DELEGATE_TIMEOUT_S,
        check=False,
    )
    return done.returncode, done.stdout, done.stderr


class PinnedDelegateTransport:
    """One ``onex delegate`` pinned to a backend, run as the dogfood tenant.

    The eval holds no provider key. The delegation's effect boundary resolves
    the tenant's registered reference, so the key is read where every customer's
    is. A run that answers as another tenant, or on a credential that is not a
    tenant's own, is an error row and never a graded answer (OMN-20974).
    """

    def __init__(
        self,
        run: DelegateRun = _run_subprocess,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._run = run
        self._environ = os.environ if environ is None else environ

    def complete(self, rung: ModelRungSpec, prompt: str) -> ModelTransportResult:
        if rung.backend_id_env is None or rung.tenant_env is None:
            raise ValueError(f"{rung.rung_id}: no backend pin declared")
        try:
            backend_id = self._environ[rung.backend_id_env]
            tenant = self._environ[rung.tenant_env]
        except KeyError as exc:
            raise RungUnresolvedError(
                f"{rung.rung_id}: environment variable {exc.args[0]} is unset"
            ) from exc
        env = {k: v for k, v in self._environ.items() if k != "PYTHONPATH"}
        env[_TENANT_ENV] = tenant
        started = time.monotonic()
        code, stdout, stderr = self._run(
            [
                _onex_executable(),
                "delegate",
                "--json",
                "--backend-id",
                backend_id,
                "--task-type",
                _DELEGATE_TASK_TYPE,
                "--bus",
                "kafka",
                "--lane",
                "dev",
                "--locus",
                "deployed-lane",
                "-",
            ],
            prompt,
            env,
        )
        if code != 0:
            tail = (stderr.strip().splitlines() or [""])[-1][:300]
            raise RuntimeError(f"onex delegate exited with status {code}: {tail}")
        result = _receipt_result(stdout)
        answered_as = result.get("tenant_id")
        if answered_as != tenant:
            raise ValueError(
                f"{rung.rung_id}: answered as tenant {answered_as!r}, not the "
                "configured dogfood tenant"
            )
        ref = result.get("secret_ref")
        if not isinstance(ref, str) or not is_tenant_credential_ref(ref):
            raise ValueError(
                f"{rung.rung_id}: the run did not use a tenant credential reference"
            )
        metrics = result.get("metrics")
        latency = metrics.get("latency_ms") if isinstance(metrics, dict) else None
        return ModelTransportResult(
            text=str(result["response"]),
            model_id=str(result.get("model_name") or backend_id),
            latency_ms=(
                latency
                if isinstance(latency, int)
                else int((time.monotonic() - started) * 1000)
            ),
            tenant_id=answered_as,
            credential_ref=ref,
        )


def _receipt_result(stdout: str) -> dict[str, object]:
    """The ``result`` of the typed receipt ``onex delegate --json`` prints."""
    for line in reversed(stdout.splitlines()):
        if not line.startswith("{"):
            continue
        try:
            receipt = json.loads(line)
        except ValueError:
            continue
        result = receipt.get("result") if isinstance(receipt, dict) else None
        if isinstance(result, dict) and "response" in result:
            return result
    raise ValueError("onex delegate printed no receipt with a response")


class RungTransport:
    """Send a rung to the way it is reached: a backend pin, or its own endpoint."""

    def __init__(
        self, pinned: Transport | None = None, chat: Transport | None = None
    ) -> None:
        self._pinned = pinned or PinnedDelegateTransport()
        self._chat = chat or OpenAIChatTransport()

    def complete(self, rung: ModelRungSpec, prompt: str) -> ModelTransportResult:
        transport = self._pinned if rung.backend_id_env is not None else self._chat
        return transport.complete(rung, prompt)
