#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CI gate: a contract's declared table ``access`` must cover what its handler does.

OMN-16690. A ``db_io`` contract declares, per table, an ``access`` capability
(``read`` / ``write`` / ``read_write``). The runtime enforces that declaration
fail-closed at the operation seam --
``omnibase_infra.runtime.auto_wiring.handler_wiring.ProjectionTableOperation``
raises ``PermissionError`` from ``_assert_read_declared`` /
``_assert_write_declared`` when the handler asks for a capability the contract
did not declare.

The defect class this gate closes: a projection handler that performs an
**idempotency read before writing** (``db.query(TABLE, ...)`` then
``db.upsert(TABLE, ...)``) under a contract that declares only ``access:
write``. Every such event is refused at the read, the projection arm's generic
``except Exception`` routes the envelope to the platform quarantine sink
(``onex.dlq.omnibase-infra.quarantine.v1``), the offset commits, consumer lag
reads 0 -- and **zero rows are ever written**. The gateway still answers 202,
so the caller sees success. It is a silent black hole: the contract promised a
capability strictly narrower than the one the code is built to use, so the code
path could never have written a row.

Doctrine: the contract must declare what the handler actually does. The fix for
a violation is to WIDEN the declaration to ``read_write`` (or to remove the
read from the handler if the read is genuinely unnecessary) -- never to weaken
the runtime guard, which is the only thing standing between an undeclared
capability and production.

WHY A STATIC GATE AND NOT JUST A TEST. The live failure is invisible to
isolation tests: every projection unit test in this repo injects a *fake*
adapter that implements ``upsert``/``query`` without the access enforcement the
real ``ProjectionDatabaseOperations`` applies. So the handler's read passes in
CI and refuses in production. Detection has to be static, over the contract and
the handler source, or it does not fire at all (Rule 5 -- detection that is not
a pre-merge gate gets ignored).

SCOPE. Every ``src/omnimarket/nodes/*/contract.yaml`` declaring
``db_io.db_tables``. Production handler modules only -- ``tests/`` and
``test_*.py`` are excluded, because a test's ``db.query(...)`` against its own
fake adapter says nothing about the handler's runtime capability needs (and
several nodes deliberately assert *no* read happens).

ESCAPE HATCH. A handler line carrying ``# projection-access-ok: <reason>``
is skipped. Reserved for a call the resolver mis-attributes (e.g. a table name
built at runtime); never for an actual undeclared capability.

Exit codes: 0 = clean; 1 = a declaration does not cover the handler's
operations; 2 = invocation error (run from repo root).
"""

from __future__ import annotations

import pathlib
import re
import sys
from dataclasses import dataclass

import yaml

_NODES_GLOB = "src/omnimarket/nodes/*/contract.yaml"

_READ_OK = {"read", "read_write"}
_WRITE_OK = {"write", "read_write"}

_ALLOW_MARKER = "# projection-access-ok:"

# Module-level ``NAME = "literal"`` (optionally annotated). Resolved PER FILE:
# several nodes ship multiple handler modules that each define their own
# ``TABLE``, so a repo- or node-wide constant map silently mis-attributes a
# read to the wrong table (and hides real violations).
_CONST_RE = re.compile(
    r'^([A-Za-z_][A-Za-z_0-9]*)\s*(?::[^=\n]+)?=\s*(["\'])([^"\']+)\2', re.MULTILINE
)

# ``.query(FIRST_ARG`` / ``.upsert(FIRST_ARG`` -- first positional argument is
# the table name in the ``DatabaseAdapter`` protocol
# (``upsert(table, conflict_key, row)`` / ``query(table, filters)``).
_CALL_RE_TMPL = r'\.{op}\(\s*\n?\s*([A-Za-z_0-9"\'.]+)'


@dataclass(frozen=True)
class Violation:
    node: str
    table: str
    declared: str
    operation: str
    sites: tuple[str, ...]

    def render(self) -> str:
        where = ", ".join(self.sites)
        needed = "read_write" if self.operation == "read" else "write"
        return (
            f"{self.node}: table {self.table!r} declares access={self.declared!r} "
            f"but the handler performs a {self.operation.upper()} at {where}. "
            f"The runtime refuses this with PermissionError and quarantines every "
            f"event. Declare access: {needed} in contract.yaml (or drop the "
            f"{self.operation})."
        )


def _resolve_constants(src: str) -> dict[str, str]:
    return {m.group(1): m.group(3) for m in _CONST_RE.finditer(src)}


def _handler_sources(node_dir: pathlib.Path) -> list[pathlib.Path]:
    """Production modules only: a test's fake-adapter call proves nothing."""
    return sorted(
        p
        for p in node_dir.rglob("*.py")
        if "tests" not in p.parts and not p.name.startswith("test_")
    )


def _calls(src: str, op: str, rel: str) -> dict[str, list[str]]:
    """Map resolved table name -> call sites for one operation in one file."""
    consts = _resolve_constants(src)
    found: dict[str, list[str]] = {}
    for match in re.finditer(_CALL_RE_TMPL.format(op=op), src):
        line_no = src.count("\n", 0, match.start()) + 1
        line = src.splitlines()[line_no - 1] if line_no <= len(src.splitlines()) else ""
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
            # Unresolvable first argument. Recorded under a sentinel so a
            # write-only table in the same node is reported rather than
            # silently passed -- fail closed, not open.
            table = f"<unresolved:{arg}>"
        found.setdefault(table, []).append(f"{rel}:{line_no}")
    return found


def scan(repo_root: pathlib.Path) -> list[Violation]:
    """Return every contract declaration narrower than its handler's usage."""
    violations: list[Violation] = []
    for contract_path in sorted(repo_root.glob(_NODES_GLOB)):
        node_dir = contract_path.parent
        try:
            raw = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:  # pragma: no cover - malformed contract
            raise SystemExit(f"ERROR: cannot parse {contract_path}: {exc}") from exc
        if not isinstance(raw, dict):
            continue
        db_io = raw.get("db_io") or {}
        tables = db_io.get("db_tables") or []
        if not tables:
            continue
        declared: dict[str, str] = {
            str(t["name"]): str(t.get("access"))
            for t in tables
            if isinstance(t, dict) and t.get("name")
        }

        reads: dict[str, list[str]] = {}
        writes: dict[str, list[str]] = {}
        for module in _handler_sources(node_dir):
            src = module.read_text(encoding="utf-8")
            rel = str(module.relative_to(repo_root))
            for table, sites in _calls(src, "query", rel).items():
                reads.setdefault(table, []).extend(sites)
            for table, sites in _calls(src, "upsert", rel).items():
                writes.setdefault(table, []).extend(sites)

        unresolved_reads = [t for t in reads if t.startswith("<unresolved:")]

        for table_name, access in sorted(declared.items()):
            if table_name in reads and access not in _READ_OK:
                violations.append(
                    Violation(
                        node_dir.name,
                        table_name,
                        access,
                        "read",
                        tuple(reads[table_name]),
                    )
                )
            if table_name in writes and access not in _WRITE_OK:
                violations.append(
                    Violation(
                        node_dir.name,
                        table_name,
                        access,
                        "write",
                        tuple(writes[table_name]),
                    )
                )
            # A read whose table could not be resolved statically, in a node
            # holding a write-only declaration, is treated as a potential
            # violation against that declaration.
            if unresolved_reads and access not in _READ_OK:
                unresolved_sites = tuple(
                    site for key in unresolved_reads for site in sorted(reads[key])
                )
                violations.append(
                    Violation(
                        node_dir.name, table_name, access, "read", unresolved_sites
                    )
                )
    return violations


def main() -> int:
    repo_root = pathlib.Path.cwd()
    if not (repo_root / "src" / "omnimarket" / "nodes").is_dir():
        print(
            "ERROR: run from repo root (src/omnimarket/nodes not found)",
            file=sys.stderr,
        )
        return 2

    violations = scan(repo_root)
    if violations:
        print(
            "Projection contract-access gate FAILED (OMN-16690) — a db_io "
            "contract declares an access capability narrower than the one its "
            "handler uses. The runtime enforces the declaration fail-closed, so "
            "every event on these paths is refused and quarantined while the "
            "caller still sees a 202:",
            file=sys.stderr,
        )
        for violation in violations:
            print(f"  - {violation.render()}", file=sys.stderr)
        print(
            "\nFix the CONTRACT (declare what the handler does) — never weaken "
            "the runtime guard.",
            file=sys.stderr,
        )
        return 1

    print(
        "Projection contract-access gate OK: every db_io declaration covers its "
        "handler's operations."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
