# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure decisions of the tool-use rubric scoring wrapper (retired tool_use_rubric.py)."""

from __future__ import annotations

import hashlib
import json
import posixpath
from pathlib import PurePosixPath

from pydantic import JsonValue

from omnimarket.nodes.node_tool_use_rubric_compute.models.model_tool_use_rubric import (
    ModelToolUseRubricRequest,
    ModelToolUseRubricResult,
)

SCORER_MODULE = "omnimarket.delegation.rubric.tool_use_score"
VERDICT_SCHEMA = "tool-use-rubric-verdict.v1"
DISABLE_ENV = "ONEX_TOOL_USE_RUBRIC"


def _need[T](value: T | None, name: str, operation: str) -> T:
    if value is None:
        raise ValueError(f"{operation} requires {name}")
    return value


def undetermined(reason: str, detail: str = "") -> dict[str, JsonValue]:
    return {
        "outcome": "UNDETERMINED",
        "scored": False,
        "reason": reason,
        "detail": detail[-600:],
        "failed_criteria": [],
        "undetermined_criteria": [],
        "attempt_verdict": None,
    }


def _not_scored(
    summary: dict[str, JsonValue], out_path: str
) -> ModelToolUseRubricResult:
    return ModelToolUseRubricResult(
        operation="summarize",
        summary={**summary, "record": out_path},
        verdict_file={"schema": VERDICT_SCHEMA, "not_scored": summary},
    )


def _summarize(request: ModelToolUseRubricRequest) -> ModelToolUseRubricResult:
    out = _need(request.out_path, "out_path", "summarize")
    if (
        request.rubric_env if request.rubric_env is not None else "on"
    ).lower() == "off":
        return _not_scored(undetermined("disabled", f"{DISABLE_ENV}=off"), out)
    if request.scorer_python is None:
        return _not_scored(
            undetermined(
                "scorer_unavailable",
                "no interpreter with omnimarket: set ONEX_TOOL_USE_SCORER_PYTHON",
            ),
            out,
        )
    if request.run_error is not None:
        return _not_scored(undetermined("scorer_failed", request.run_error), out)
    record = request.record if request.returncode == 0 else None
    attempt = record.get("attempt_verdict") if record is not None else None
    if (
        record is None
        or "attempt_verdict" not in record
        or not isinstance(attempt, dict)
    ):
        return _not_scored(
            undetermined(
                "scorer_failed",
                f"rc={request.returncode} {request.stderr.strip()}",
            ),
            out,
        )
    return ModelToolUseRubricResult(
        operation="summarize",
        summary={
            "outcome": attempt["outcome"],
            "scored": True,
            "failed_criteria": attempt["failed_criteria"],
            "undetermined_criteria": attempt["undetermined_criteria"],
            "measured": record.get("measured"),
            "attempt_verdict": attempt,
            "record": out,
        },
    )


def _scorer_candidates(request: ModelToolUseRubricRequest) -> list[str]:
    """The interpreters to try, in the wrapper's order; unset ones are left out."""
    venv = request.dispatch_venv
    home = request.registry_root
    candidates = (
        request.scorer_python_env,
        request.lab_work_python_env,
        posixpath.join(venv, "bin", "python") if venv else None,
        posixpath.join(home, ".onex-dispatch-venv", "bin", "python") if home else None,
    )
    return [candidate for candidate in candidates if candidate]


class HandlerToolUseRubric:
    """Decide the scorer plan and the verdict summary from facts; run nothing, write nothing."""

    def handle(self, request: ModelToolUseRubricRequest) -> ModelToolUseRubricResult:
        op = request.operation
        if op == "disabled":
            value = request.rubric_env if request.rubric_env is not None else "on"
            return ModelToolUseRubricResult(operation=op, flag=value.lower() == "off")
        if op == "scorer_candidates":
            return ModelToolUseRubricResult(
                operation=op, paths=_scorer_candidates(request)
            )
        if op == "scorer_python":
            for candidate in _scorer_candidates(request):
                if candidate in request.executable_paths:
                    return ModelToolUseRubricResult(operation=op, text=candidate)
            return ModelToolUseRubricResult(operation=op)
        if op == "scorer_command":
            python = _need(request.scorer_python, "scorer_python", op)
            source = _need(request.source, "source", op)
            run_ref = _need(request.run_ref, "run_ref", op)
            out = _need(request.out_path, "out_path", op)
            return ModelToolUseRubricResult(
                operation=op,
                command=[
                    python,
                    "-m",
                    SCORER_MODULE,
                    source,
                    *request.args,
                    "--run-ref",
                    run_ref,
                    "--out",
                    out,
                ],
                env={k: v for k, v in request.environ.items() if k != "PYTHONPATH"},
            )
        if op == "manifest":
            if request.ls_returncode != 0:
                return ModelToolUseRubricResult(operation=op)
            by_path = {entry.path: entry for entry in request.entries}
            rows: list[dict[str, JsonValue]] = []
            for name in dict.fromkeys(i for i in request.ls_stdout.split("\0") if i):
                entry = by_path.get(name)
                if entry is None or not entry.is_file or entry.line_count is None:
                    continue
                rows.append({"path": name, "line_count": entry.line_count})
            return ModelToolUseRubricResult(operation=op, rows=rows)
        if op == "summarize":
            return _summarize(request)
        if op == "summary_line":
            prefix = _need(request.prefix, "prefix", op)
            summary = _need(request.summary, "summary", op)
            failed_value = summary.get("failed_criteria")
            failed = (
                ",".join(str(c) for c in failed_value)
                if isinstance(failed_value, list)
                else ""
            )
            reason = "" if summary.get("scored") else f" reason={summary.get('reason')}"
            return ModelToolUseRubricResult(
                operation=op,
                text=f"{prefix} outcome={summary['outcome']} failed={failed or '-'}{reason} record={summary.get('record')}",
            )
        if op == "claude_version":
            parts = request.version_stdout.strip().split(" ")
            return ModelToolUseRubricResult(operation=op, text=parts[0] or "unknown")
        if op == "schema_cache_target":
            version = _need(request.version, "version", op)
            key = hashlib.sha256(
                json.dumps([version, request.flags]).encode()
            ).hexdigest()[:16]
            if request.cache_dir:
                cache = PurePosixPath(request.cache_dir)
            else:
                base = request.xdg_cache_home or str(
                    PurePosixPath(_need(request.home, "home", op)) / ".cache"
                )
                cache = PurePosixPath(base) / "omni" / "tool-schemas"
            return ModelToolUseRubricResult(
                operation=op, text=str(cache / f"claude-code-{version}-{key}.json")
            )
        if op == "pick_captured_tools":
            captured = [
                tools
                for body in request.captured_bodies
                if isinstance(tools := body.get("tools"), list)
            ]
            if not captured:
                return ModelToolUseRubricResult(operation=op)
            return ModelToolUseRubricResult(operation=op, tools=max(captured, key=len))
        raise ValueError(f"unknown operation {op!r}")
