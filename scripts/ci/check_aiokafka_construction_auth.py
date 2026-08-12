#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CI gate: every direct aiokafka client construction must carry MSK-IAM auth kwargs.

OMN-15833 found ~20 direct ``AIOKafkaConsumer(``/``AIOKafkaProducer(`` construction
sites in ``omnimarket`` (node consumers, an adapter, and standalone publisher
scripts) that never applied ``security_protocol``/``sasl_mechanism`` and would
silently connect PLAINTEXT against a SASL/IAM-only MSK broker. This is the
**third** occurrence of the class: the original ``omnibase_infra`` sweep
(OMN-14155) never scanned ``omnimarket``, and ``omnimarket``'s own first fix
(OMN-15816, ``omnimarket#2037``) covered only the two ``projection/`` sites
it was scoped to.

This gate closes the class for the whole repo, and — per OMN-14158, which
documents the exact bypass in the ``omnibase_infra`` guard this gate is
modeled on — does so at **call-site** granularity, not file level:

    OMN-14158: "``_file_references_auth_helper()`` has a 'belt-and-suspenders'
    fallback ``any(helper in source for helper in _AUTH_HELPER_NAMES)`` that
    treats the helper name appearing *anywhere in the file* (comment, dead/
    aliased import, docstring) as 'wired'. [...] It's also file-level not
    call-site-level (a 2-construction file with only 1 wired passes)."

There is no substring/file-level fallback here. A guarded ``Call`` is only
considered "authed" if the auth-kwargs helper is invoked as a keyword on
*that exact* ``Call`` node — either a ``**helper()`` dict-unpack, or an
explicit ``security_protocol=`` keyword recording a deliberate (non-default)
choice. A file that imports or mentions the helper without wiring it into
the specific constructor call still fails.

Scope: ``src/omnimarket/`` (runtime node/adapter code) and ``scripts/``
(operational publisher CLIs) — the two trees that ship code capable of
connecting to a real broker. ``tests/`` is out of scope: test fixtures
intentionally construct PLAINTEXT clients against a local/ephemeral
Redpanda, same boundary the OMN-14155 ``omnibase_infra`` guard draws by
scanning only its ``src/`` tree.

Exit codes: 0 = clean; 1 = violations found; 2 = invocation error (run from
repo root).
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SCAN_ROOTS = (_REPO_ROOT / "src" / "omnimarket", _REPO_ROOT / "scripts")

_GUARDED_CALL_NAMES: frozenset[str] = frozenset(
    {"AIOKafkaConsumer", "AIOKafkaProducer", "AIOKafkaAdminClient"}
)

_AUTH_HELPER_NAMES: frozenset[str] = frozenset(
    {
        "build_aiokafka_auth_kwargs",
        "build_aiokafka_auth_kwargs_from_env",
    }
)

# Explicit kwargs that, present directly on the guarded Call, record a
# deliberate auth decision even without going through the shared helper.
_EXPLICIT_AUTH_KWARGS: frozenset[str] = frozenset({"security_protocol"})

# Files that construct a guarded client but are explicitly out of scope.
# Every entry here must carry a reason — this is an allowlist of known,
# reviewed exceptions, not a place to silence new findings. Path is relative
# to the repo root.
_ALLOWLIST: dict[str, str] = {}


def _iter_python_files() -> list[Path]:
    files: list[Path] = []
    for root in _SCAN_ROOTS:
        if root.exists():
            files.extend(root.rglob("*.py"))
    return sorted(set(files))


def _relative(path: Path) -> str:
    try:
        return str(path.relative_to(_REPO_ROOT))
    except ValueError:
        # Path isn't under the configured repo root (e.g. a unit test scanning
        # a file directly via `_scan_file()` without going through `main()`'s
        # `_SCAN_ROOTS`/`_REPO_ROOT` pairing). Fall back to the raw path — this
        # only affects the human-readable label, never the scan/allowlist logic.
        return str(path)


def _call_name(node: ast.expr) -> str | None:
    """Resolve a Call's callee to a simple name, handling `mod.helper()` too."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _find_guarded_calls(tree: ast.AST) -> list[ast.Call]:
    calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node.func)
        if name in _GUARDED_CALL_NAMES:
            calls.append(node)
    return calls


def _call_is_authed(call: ast.Call) -> bool:
    """Call-site-level check: does *this* Call carry auth evidence directly.

    Deliberately does NOT fall back to a file-wide substring/import check —
    that is the exact OMN-14158 bypass this gate exists to close.
    """
    for kw in call.keywords:
        # `**build_aiokafka_auth_kwargs_from_env()` / `**mod.build_...()`
        if kw.arg is None and isinstance(kw.value, ast.Call):
            helper_name = _call_name(kw.value.func)
            if helper_name in _AUTH_HELPER_NAMES:
                return True
        # explicit `security_protocol=...` recorded directly on this call
        if kw.arg in _EXPLICIT_AUTH_KWARGS:
            return True
    return False


def _scan_file(path: Path) -> list[str]:
    """Return violation descriptions ('relpath:lineno') for one file."""
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return []

    violations = []
    for call in _find_guarded_calls(tree):
        if not _call_is_authed(call):
            name = _call_name(call.func)
            violations.append(f"{_relative(path)}:{call.lineno}: bare {name}(...)")
    return violations


def main() -> int:
    if not any(root.exists() for root in _SCAN_ROOTS):
        print(
            f"ERROR: none of the scan roots exist: {[str(r) for r in _SCAN_ROOTS]}. "
            "Run this script from the omnimarket repo root.",
            file=sys.stderr,
        )
        return 2

    all_violations: list[str] = []
    stale_allowlist: list[str] = []

    for path in _iter_python_files():
        rel = _relative(path)
        violations = _scan_file(path)

        if rel in _ALLOWLIST:
            if not violations:
                stale_allowlist.append(rel)
            continue

        all_violations.extend(violations)

    if stale_allowlist:
        print(
            "ERROR: stale OMN-15833 allowlist entries (no longer construct a "
            f"guarded client without auth) — remove them: {stale_allowlist}",
            file=sys.stderr,
        )
        return 1

    if all_violations:
        print(
            "Direct aiokafka client construction without MSK-IAM auth kwargs "
            "(OMN-15833 regression guard):",
            file=sys.stderr,
        )
        for v in all_violations:
            print(f"  {v}", file=sys.stderr)
        print(
            "\nEither spread `**build_aiokafka_auth_kwargs_from_env()` (from "
            "omnibase_infra.event_bus.kafka_auth) into the SAME constructor call, "
            "pass an explicit `security_protocol=` kwarg on that call, or add the "
            "file to `_ALLOWLIST` in this script with a documented reason.",
            file=sys.stderr,
        )
        return 1

    print(f"OK: no bare aiokafka client construction ({_REPO_ROOT}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
