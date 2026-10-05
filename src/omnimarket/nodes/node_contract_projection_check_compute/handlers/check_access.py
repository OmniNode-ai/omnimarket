# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Rule ``access``: a contract's declared table access must cover its handler's operations.

Ported from ``scripts/ci/check_projection_contract_access.py`` (OMN-16690). A
``db_io`` contract declares per-table ``access``; the runtime refuses a handler
that reads under ``write`` or writes under ``read`` with ``PermissionError`` and
quarantines every event while the gateway still answers 202. The fix is always
to widen the contract, never to weaken the runtime guard.

A handler line carrying the ``projection-access-ok`` marker is skipped, exactly
as the script skipped it. No new marker is introduced.
"""

import re
from collections.abc import Sequence

import yaml
from omnibase_core.models.validation.model_validation_finding import (
    ModelValidationFinding,
)

from omnimarket.nodes.node_contract_projection_check_compute.handlers.findings import (
    RULE_ACCESS,
    RULE_UNPARSEABLE,
    make_finding,
)
from omnimarket.nodes.node_contract_projection_check_compute.models import (
    ModelProjectionNodeSources,
)

_READ_OK = {"read", "read_write"}
_WRITE_OK = {"write", "read_write"}
_ALLOW_MARKER = "# projection-access" + "-ok:"
_CONST_RE = re.compile(
    r'^([A-Za-z_][A-Za-z_0-9]*)\s*(?::[^=\n]+)?=\s*(["\'])([^"\']+)\2', re.MULTILINE
)
_CALL_RE_TMPL = r'\.{op}\(\s*\n?\s*([A-Za-z_0-9"\'.]+)'


def _render(
    node: str, table: str, declared: str, operation: str, sites: tuple[str, ...]
) -> str:
    where = ", ".join(sites)
    needed = "read_write" if operation == "read" else "write"
    return (
        f"{node}: table {table!r} declares access={declared!r} "
        f"but the handler performs a {operation.upper()} at {where}. "
        f"The runtime refuses this with PermissionError and quarantines every "
        f"event. Declare access: {needed} in contract.yaml (or drop the "
        f"{operation})."
    )


def _calls(src: str, op: str, rel: str) -> dict[str, list[str]]:
    consts = {m.group(1): m.group(3) for m in _CONST_RE.finditer(src)}
    lines = src.splitlines()
    found: dict[str, list[str]] = {}
    for match in re.finditer(_CALL_RE_TMPL.format(op=op), src):
        line_no = src.count("\n", 0, match.start()) + 1
        line = lines[line_no - 1] if line_no <= len(lines) else ""
        if _ALLOW_MARKER in line:
            continue
        arg = match.group(1).strip()
        if arg[:1] in {'"', "'"}:
            table = arg.strip("\"'")
        elif arg in consts:
            table = consts[arg]
        elif "." in arg and arg.rsplit(".", 1)[-1] in consts:
            table = consts[arg.rsplit(".", 1)[-1]]
        else:
            table = f"<unresolved:{arg}>"
        found.setdefault(table, []).append(f"{rel}:{line_no}")
    return found


def _declared_tables(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    db_io = raw.get("db_io") or {}
    tables = db_io.get("db_tables") or []
    return {
        str(t["name"]): str(t.get("access"))
        for t in tables
        if isinstance(t, dict) and t.get("name")
    }


def _add(
    findings: list[ModelValidationFinding],
    node: ModelProjectionNodeSources,
    table: str,
    access: str,
    operation: str,
    sites: tuple[str, ...],
) -> None:
    findings.append(
        make_finding(
            rule_id=RULE_ACCESS,
            message=_render(node.node, table, access, operation, sites),
            location=sites[0] if sites else node.contract_path,
        )
    )


def check_access(
    nodes: Sequence[ModelProjectionNodeSources],
) -> list[ModelValidationFinding]:
    findings: list[ModelValidationFinding] = []
    for node in sorted(nodes, key=lambda n: n.contract_path.split("/")):
        try:
            raw = yaml.safe_load(node.contract_text)
        except yaml.YAMLError as exc:
            findings.append(
                make_finding(
                    rule_id=RULE_UNPARSEABLE,
                    message=f"cannot parse {node.contract_path}: {exc}",
                    location=node.contract_path,
                    severity="ERROR",
                )
            )
            continue
        declared = _declared_tables(raw)
        if not declared:
            continue
        reads: dict[str, list[str]] = {}
        writes: dict[str, list[str]] = {}
        for module in sorted(node.modules, key=lambda m: m.path.split("/")):
            for table, sites in _calls(module.source, "query", module.path).items():
                reads.setdefault(table, []).extend(sites)
            for table, sites in _calls(module.source, "upsert", module.path).items():
                writes.setdefault(table, []).extend(sites)
        unresolved_reads = [t for t in reads if t.startswith("<unresolved:")]

        for table_name, access in sorted(declared.items()):
            if table_name in reads and access not in _READ_OK:
                _add(
                    findings, node, table_name, access, "read", tuple(reads[table_name])
                )
            if table_name in writes and access not in _WRITE_OK:
                _add(
                    findings,
                    node,
                    table_name,
                    access,
                    "write",
                    tuple(writes[table_name]),
                )
            if unresolved_reads and access not in _READ_OK:
                _add(
                    findings,
                    node,
                    table_name,
                    access,
                    "read",
                    tuple(
                        site for key in unresolved_reads for site in sorted(reads[key])
                    ),
                )
    return findings
