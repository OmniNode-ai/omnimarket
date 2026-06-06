# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""HandlerShimScanner — AST-based @shim decorator scanner.

ONEX node type: COMPUTE (pure — reads files, returns findings, no side effects).
"""

from __future__ import annotations

import ast
import datetime
from pathlib import Path

from omnimarket.nodes.node_shim_scanner.models.model_shim_finding import (
    EnumShimStatus,
    ModelShimFinding,
)
from omnimarket.nodes.node_shim_scanner.models.model_shim_scan_request import (
    ModelShimScanRequest,
)
from omnimarket.nodes.node_shim_scanner.models.model_shim_scan_result import (
    ModelShimScanResult,
)

__all__ = ["HandlerShimScanner"]

_SHIM_DECORATOR_NAME = "shim"


class HandlerShimScanner:
    """Pure COMPUTE handler: walk paths, parse AST, extract @shim annotations."""

    def handle(self, request: ModelShimScanRequest) -> ModelShimScanResult:
        reference_date = (
            datetime.date.fromisoformat(request.reference_date)
            if request.reference_date
            else datetime.date.today()
        )

        py_files = _collect_python_files(request.paths)
        findings: list[ModelShimFinding] = []

        for file_path in py_files:
            findings.extend(
                _scan_file(
                    file_path=file_path,
                    reference_date=reference_date,
                    warn_days_before_expiry=request.warn_days_before_expiry,
                )
            )

        expired = sum(1 for f in findings if f.status == EnumShimStatus.EXPIRED)
        expiring = sum(1 for f in findings if f.status == EnumShimStatus.EXPIRING)

        return ModelShimScanResult(
            findings=findings,
            expired_count=expired,
            expiring_count=expiring,
            total_count=len(findings),
        )


def _collect_python_files(paths: list[str]) -> list[Path]:
    result: list[Path] = []
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            result.extend(sorted(p.rglob("*.py")))
        elif p.is_file() and p.suffix == ".py":
            result.append(p)
    return result


def _scan_file(
    file_path: Path,
    reference_date: datetime.date,
    warn_days_before_expiry: int,
) -> list[ModelShimFinding]:
    try:
        source = file_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(file_path))
    except (SyntaxError, OSError):
        return []

    findings: list[ModelShimFinding] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            extracted = _extract_shim_args(decorator)
            if extracted is None:
                continue
            ticket_id, expires_on, reason, replacement = extracted
            delta = (expires_on - reference_date).days
            if delta < 0:
                status = EnumShimStatus.EXPIRED
            elif delta <= warn_days_before_expiry:
                status = EnumShimStatus.EXPIRING
            else:
                status = EnumShimStatus.ACTIVE
            findings.append(
                ModelShimFinding(
                    file_path=str(file_path),
                    line_number=node.lineno,
                    function_name=node.name,
                    ticket_id=ticket_id,
                    expires_on=expires_on,
                    reason=reason,
                    replacement=replacement,
                    status=status,
                    days_until_expiry=delta,
                )
            )
    return findings


def _extract_shim_args(
    decorator: ast.expr,
) -> tuple[str, datetime.date, str, str] | None:
    """Return (ticket_id, expires_on, reason, replacement) or None."""
    # Support both @shim(...) and @module.shim(...)
    if isinstance(decorator, ast.Call):
        func = decorator.func
        name = (
            func.id
            if isinstance(func, ast.Name)
            else func.attr
            if isinstance(func, ast.Attribute)
            else None
        )
        if name != _SHIM_DECORATOR_NAME:
            return None
        return _parse_shim_call(decorator)
    return None


def _parse_shim_call(
    call: ast.Call,
) -> tuple[str, datetime.date, str, str] | None:
    """Parse keyword or positional args from a @shim(...) call node."""
    kwargs: dict[str, ast.expr] = {kw.arg: kw.value for kw in call.keywords if kw.arg}
    positional = call.args

    def _get(name: str, pos: int) -> ast.expr | None:
        if name in kwargs:
            return kwargs[name]
        if pos < len(positional):
            return positional[pos]
        return None

    ticket_node = _get("ticket_id", 0)
    expires_node = _get("expires_on", 1)
    reason_node = _get("reason", 2)
    replacement_node = _get("replacement", 3)

    if any(
        n is None for n in (ticket_node, expires_node, reason_node, replacement_node)
    ):
        return None

    ticket_id = _str_value(ticket_node)  # type: ignore[arg-type]
    reason = _str_value(reason_node)  # type: ignore[arg-type]
    replacement = _str_value(replacement_node)  # type: ignore[arg-type]
    expires_on = _date_value(expires_node)  # type: ignore[arg-type]

    if any(v is None for v in (ticket_id, reason, replacement, expires_on)):
        return None

    return ticket_id, expires_on, reason, replacement  # type: ignore[return-value]


def _str_value(node: ast.expr) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _date_value(node: ast.expr) -> datetime.date | None:
    """Parse datetime.date(Y, M, D) call node into a date."""
    if not isinstance(node, ast.Call):
        return None
    # Accept datetime.date(...) or date(...)
    func = node.func
    name = (
        func.attr
        if isinstance(func, ast.Attribute)
        else func.id
        if isinstance(func, ast.Name)
        else None
    )
    if name != "date":
        return None
    args = node.args
    if len(args) != 3:
        return None
    parts = [
        a.value
        for a in args
        if isinstance(a, ast.Constant) and isinstance(a.value, int)
    ]
    if len(parts) != 3:
        return None
    try:
        return datetime.date(parts[0], parts[1], parts[2])
    except ValueError:
        return None
