# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The two-backend suite: run every listed effect on both backends (OMN-22911).

An effect declares ``backends.in_memory.factory``, ``backends.real.factory`` and
``backends.suite.scenario``. A factory is ``factory(rows, workdir) -> backend``; the scenario is
``scenario(factory, workdir) -> result`` and owns its inputs. The suite runs the scenario once per
backend and compares what comes back: green only when both pass with equal results. Effects not on
both backends are not run; they are counted, because the count is what the reading carries.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import yaml

from omnimarket.nodes.node_two_backend_suite_effect.models.model_two_backend_suite import (
    EnumTwoBackendVerdict,
    ModelEffectBackendEntry,
    ModelEffectRun,
    ModelTwoBackendRequest,
    ModelTwoBackendResult,
)
from omnimarket.nodes.node_two_backend_suite_effect.protocols import (
    ProtocolDottedResolver,
    ProtocolEffectBackendReportSource,
)

CONTRACT = Path(__file__).resolve().parents[1] / "contract.yaml"
_MS_PER_S = 1000
IN_MEMORY = "in_memory"
REAL = "real"


def load_topics(path: Path = CONTRACT) -> dict[str, str]:
    """Topics by role from the node's contract."""
    return dict(yaml.safe_load(path.read_text(encoding="utf-8"))["topics"])


def load_timeout_s(path: Path = CONTRACT) -> float:
    """The bound on one backend attempt: the contract's declared ``descriptor.timeout_ms``, in seconds."""
    descriptor = yaml.safe_load(path.read_text(encoding="utf-8"))["descriptor"]
    return float(descriptor["timeout_ms"]) / _MS_PER_S


class HandlerTwoBackendSuite:
    def __init__(
        self,
        *,
        source: ProtocolEffectBackendReportSource,
        resolver: ProtocolDottedResolver,
        workdir: Path,
        timeout_s: float | None = None,
    ) -> None:
        self._source = source
        self._resolver = resolver
        self._workdir = workdir
        self._timeout_s = load_timeout_s() if timeout_s is None else timeout_s

    def _bounded(self, call: Callable[[], str]) -> str:
        """Run one attempt on a daemon thread and raise ``TimeoutError`` when it does not return inside the bound.

        The stalled call is abandoned, not killed; the suite goes on to the next effect and the timeout is the attempt's
        visible verdict (``fail:TimeoutError``).
        """
        box: dict[str, object] = {}

        def target() -> None:
            try:
                box["value"] = call()
            except BaseException as exc:  # carried to the caller and raised there
                box["error"] = exc

        worker = threading.Thread(target=target, daemon=True)
        worker.start()
        worker.join(self._timeout_s)
        if worker.is_alive():
            raise TimeoutError(f"no result inside {self._timeout_s} s")
        if "error" in box:
            error = box["error"]
            assert isinstance(error, BaseException)
            raise error
        value = box["value"]
        assert isinstance(value, str)
        return value

    def handle(self, request: ModelTwoBackendRequest) -> ModelTwoBackendResult:
        read = self._source.read()
        if read.report is None:
            return ModelTwoBackendResult(
                correlation_id=request.correlation_id,
                status="failed",
                failure=read.error or "report-unreadable",
            )
        if read.report.incomplete_cause:
            return ModelTwoBackendResult(
                correlation_id=request.correlation_id,
                status="failed",
                failure=read.report.incomplete_cause,
            )
        runs = tuple(self._run(entry) for entry in read.report.entries if entry.on_both)
        return ModelTwoBackendResult(
            correlation_id=request.correlation_id,
            status="completed",
            runs=runs,
            not_on_both_count=read.report.not_on_both_count,
        )

    def _attempt(
        self, entry: ModelEffectBackendEntry, backend: str, factory_path: str
    ) -> tuple[str, str]:
        """(``pass`` or ``fail:<ErrorType>``, the JSON of what the scenario returned)."""
        workdir = self._workdir / entry.node_name / backend
        try:
            workdir.mkdir(parents=True, exist_ok=True)
            scenario = self._resolver.resolve(entry.scenario)
            factory = self._resolver.resolve(factory_path)
            encoded = self._bounded(
                lambda: json.dumps(
                    scenario(factory, workdir), sort_keys=True, default=str
                )
            )
            return "pass", encoded
        except Exception as exc:  # a backend failing is the finding, whatever its type
            return f"fail:{type(exc).__name__}", ""

    def _run(self, entry: ModelEffectBackendEntry) -> ModelEffectRun:
        memory, memory_result = self._attempt(entry, IN_MEMORY, entry.in_memory_factory)
        real, real_result = self._attempt(entry, REAL, entry.real_factory)
        if memory == "pass" and real == "pass":
            verdict = (
                EnumTwoBackendVerdict.BOTH_PASS
                if memory_result == real_result
                else EnumTwoBackendVerdict.DIVERGED
            )
        elif memory == "pass" or real == "pass":
            verdict = EnumTwoBackendVerdict.ONE_BACKEND_ONLY
        else:
            verdict = EnumTwoBackendVerdict.BOTH_FAIL
        return ModelEffectRun(
            node_name=entry.node_name, verdict=verdict, in_memory=memory, real=real
        )


def events_of(
    result: ModelTwoBackendResult, topics: dict[str, str]
) -> list[tuple[str, dict[str, object]]]:
    """The events a run produces, in order, all under its correlation id."""
    if result.status == "failed":
        return [
            (
                topics["two_backend_suite_failed"],
                {"correlation_id": result.correlation_id, "failure": result.failure},
            )
        ]
    ordered = sorted(
        result.runs,
        key=lambda r: (r.verdict is EnumTwoBackendVerdict.BOTH_PASS, r.node_name),
    )
    events: list[tuple[str, dict[str, object]]] = [
        (
            topics[
                "two_backend_effect_passed"
                if run.verdict is EnumTwoBackendVerdict.BOTH_PASS
                else "two_backend_effect_failed"
            ],
            {
                "correlation_id": result.correlation_id,
                "node_name": run.node_name,
                "verdict": run.verdict.value,
                "in_memory": run.in_memory,
                "real": run.real,
            },
        )
        for run in ordered
    ]
    events.append(
        (
            topics["two_backend_suite_completed"],
            {
                "correlation_id": result.correlation_id,
                "run": len(result.runs),
                "failed": sum(
                    1
                    for r in result.runs
                    if r.verdict is not EnumTwoBackendVerdict.BOTH_PASS
                ),
                "not_on_both": result.not_on_both_count,
            },
        )
    )
    return events


def build_status_row(
    result: ModelTwoBackendResult, *, lane: str, actor: str, at: datetime
) -> str:
    """The STATUS row the piece 4 reading reads: ``red`` is the count of effects not green, ``unread`` when no list."""
    red = (
        "unread"
        if result.status == "failed"
        else str(
            sum(
                1
                for r in result.runs
                if r.verdict is not EnumTwoBackendVerdict.BOTH_PASS
            )
        )
    )
    stamp = at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    cells = [
        f"lane={lane}",
        f"actor={actor}",
        "model=none",
        "src=two-backend-suite",
        f"outcome={result.status}",
        f"run={len(result.runs)}",
        f"red={red}",
        f"not_on_both={result.not_on_both_count}",
    ]
    return f"{stamp} | STATUS | " + " | ".join(cells) + " | two-backend suite run"
