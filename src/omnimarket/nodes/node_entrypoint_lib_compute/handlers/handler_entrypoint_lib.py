# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure decisions of the shared one-command skill entrypoint helpers (retired entrypoint_lib.sh)."""

from __future__ import annotations

import re

from omnimarket.nodes.node_entrypoint_lib_compute.models.model_entrypoint_lib import (
    ModelEntrypointLibRequest,
    ModelEntrypointLibResult,
)

_PRESERVED = re.compile(r"^(OK |REFUSED |RETRY |FIX: |NOT-APPENDED )")
_REFUSED_OR_RETRY = re.compile(r"^(REFUSED|RETRY) ")
_APPEND_VERBS = ("row", "append-rows")


def _need(value: str | None, name: str, operation: str) -> str:
    if value is None:
        raise ValueError(f"{operation} requires {name}")
    return value


def _need_int(value: int | None, name: str, operation: str) -> int:
    if value is None:
        raise ValueError(f"{operation} requires {name}")
    return value


def _refusal(
    operation: str,
    code: int,
    rule: str,
    reason: str,
    fix: str,
    timing_line: str | None,
) -> ModelEntrypointLibResult:
    """entry_refuse: two lines on stdout, the TIMING line on stderr when started, then exit."""
    return ModelEntrypointLibResult.model_validate(
        {
            "operation": operation,
            "stdout": [f"REFUSED {code} {rule}: {reason}", f"FIX: {fix}"],
            "stderr": [timing_line] if timing_line else [],
            "exit_code": code,
        }
    )


class HandlerEntrypointLib:
    """Decide lines and exit codes from facts; read no environment, run no process, keep no clock."""

    def handle(self, request: ModelEntrypointLibRequest) -> ModelEntrypointLibResult:
        op = request.operation
        timing = request.timing_line
        if op == "refuse":
            return _refusal(
                op,
                _need_int(request.code, "code", op),
                _need(request.rule, "rule", op),
                _need(request.reason, "reason", op),
                _need(request.fix, "fix", op),
                timing,
            )
        if op == "dry_run_guard":
            if not request.onex_dry_run:
                return ModelEntrypointLibResult(operation=op)
            if request.registry_root and request.sandbox_marker_exists:
                return ModelEntrypointLibResult(operation=op)
            return _refusal(
                op,
                2,
                "dry-run-guard",
                "ONEX_DRY_RUN set outside a sandbox registry root",
                "set OMNI_HOME to a sandbox with .dryrun-sandbox, or unset ONEX_DRY_RUN",
                timing,
            )
        if op == "require_env":
            if not request.registry_root:
                return _refusal(
                    op,
                    2,
                    "env",
                    "OMNI_HOME is required",
                    "set OMNI_HOME to the registry root",
                    timing,
                )
            if not request.ledger_path:
                return _refusal(
                    op,
                    2,
                    "env",
                    "ONEX_LEDGER_PATH is required",
                    "set ONEX_LEDGER_PATH to the rolling work ledger",
                    timing,
                )
            return ModelEntrypointLibResult(operation=op)
        if op == "detail_dir":
            if request.detail_dir_override:
                return ModelEntrypointLibResult(
                    operation=op, value=request.detail_dir_override
                )
            home = _need(request.registry_root, "registry_root", op)
            return ModelEntrypointLibResult(
                operation=op, value=f"{home}/.claude_scratch/ledger-detail"
            )
        if op == "verb_budget":
            if request.budget_override:
                return ModelEntrypointLibResult(
                    operation=op, value=request.budget_override
                )
            verb = _need(request.verb, "verb", op)
            return ModelEntrypointLibResult(
                operation=op, value="90" if verb in _APPEND_VERBS else "60"
            )
        if op == "start_line":
            name = request.entry_name or "entry"
            path = _need(request.detail_path, "detail_path", op)
            return ModelEntrypointLibResult(
                operation=op, stderr=[f"STEP {name} detail={path}"]
            )
        if op == "step_start_line":
            step = _need(request.step, "step", op)
            budget = (
                f" budget={request.budget_seconds}s" if request.budget_seconds else ""
            )
            return ModelEntrypointLibResult(
                operation=op, stderr=[f"STEP {step} start{budget}"]
            )
        if op == "step_end_line":
            step = _need(request.step, "step", op)
            rc = _need_int(request.rc, "rc", op)
            took = _need(request.took_seconds, "took_seconds", op)
            return ModelEntrypointLibResult(
                operation=op, stderr=[f"STEP {step} done rc={rc} {took}s"]
            )
        if op == "timing_line":
            name = request.entry_name or "entry"
            total = _need(request.total_seconds, "total_seconds", op)
            steps = "".join(f" {s.name}={s.seconds}s" for s in request.steps)
            return ModelEntrypointLibResult(
                operation=op, value=f"TIMING {name} total={total}s{steps}"
            )
        if op == "too_old":
            path = _need(request.detail_path, "detail_path", op)
            return _refusal(
                op,
                2,
                "onex-ledger-protocol",
                f"the ledger command returned no expected result; detail={path}",
                f"read {path} and repair the reported command or protocol failure before retrying",
                timing,
            )
        if op == "verb_failed":
            rc = _need_int(request.rc, "rc", op)
            path = _need(request.detail_path, "detail_path", op)
            lines = _need(request.detail_text, "detail_text", op).splitlines()
            kept = [line for line in lines if _PRESERVED.match(line)]
            if not any(_REFUSED_OR_RETRY.match(line) for line in lines):
                result = _refusal(
                    op,
                    rc,
                    "onex-ledger-command",
                    f"the ledger command exited {rc}{request.verb_error_suffix}; detail={path}",
                    f"read {path} and repair the reported failure before retrying",
                    timing,
                )
                return result.model_copy(update={"stdout": [*kept, *result.stdout]})
            return ModelEntrypointLibResult(
                operation=op,
                stdout=kept,
                stderr=[timing] if timing else [],
                exit_code=rc,
            )
        if op == "last":
            prefix = re.compile("^" + _need(request.prefix, "prefix", op))
            lines = _need(request.detail_text, "detail_text", op).splitlines()
            hits = [line for line in lines if prefix.search(line)]
            return ModelEntrypointLibResult(
                operation=op, value=hits[-1] if hits else None
            )
        if op == "field":
            line = _need(request.line, "line", op)
            key = _need(request.key, "key", op)
            marker = f"{key}="
            for token in line.split(" "):
                if token.startswith(marker):
                    return ModelEntrypointLibResult(
                        operation=op, value=token[len(marker) :]
                    )
            return ModelEntrypointLibResult(operation=op)
        raise ValueError(f"unknown operation {op!r}")
