#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CI gate: a projection handler that can raise ValidationError needs a DLQ path.

OMN-13548 (D-03). Projection runners consume inbound bus events and validate them
into typed models. A ``ValidationError`` on a malformed event must NOT be logged
and dropped silently — it must emit a DURABLE failure signal on the bus: the
offending envelope routed to the contract-declared DLQ topic
(``event_bus.dlq_topics``), carrying the event's correlation_id.

This gate scans every projection handler module under
``src/omnimarket/nodes/node_projection_*/handlers/`` and fails any module that
references ``ValidationError`` (i.e. catches/handles validation failures) without
also wiring a DLQ route. "Wired a DLQ route" is proven by a reference to any of
the canonical DLQ surface symbols:

* ``route_to_dlq`` / ``_route_malformed_to_dlq`` — the publish call;
* ``dlq_topics`` — the contract-declared topic list.

A handler with NO ``ValidationError`` reference is out of scope (it does not
validate inbound events) and is skipped.

Exit codes: 0 = clean; 1 = a validating handler lacks a DLQ route; 2 = invocation
error (run from repo root).
"""

from __future__ import annotations

import pathlib
import sys

_HANDLERS_GLOB = "src/omnimarket/nodes/node_projection_*/handlers/handler_*.py"

# Symbols that prove a DLQ route is wired in the module.
_DLQ_ROUTE_MARKERS = (
    "route_to_dlq",
    "_route_malformed_to_dlq",
    "dlq_topics",
)

_VALIDATION_MARKER = "ValidationError"

# Inline escape hatch for a handler that legitimately re-raises (propagates) a
# ValidationError to the caller instead of catching+dropping it — propagation is
# already a durable signal, not a silent drop.
_ALLOW_MARKER = "# dlq-path-not-required:"


def _scan(repo_root: pathlib.Path) -> list[str]:
    violations: list[str] = []
    for path in sorted(repo_root.glob(_HANDLERS_GLOB)):
        text = path.read_text(encoding="utf-8")
        if _VALIDATION_MARKER not in text:
            continue
        if any(marker in text for marker in _DLQ_ROUTE_MARKERS):
            continue
        if _ALLOW_MARKER in text:
            continue
        rel = path.relative_to(repo_root)
        violations.append(
            f"{rel}: references {_VALIDATION_MARKER} but wires no DLQ route "
            f"(expected one of {', '.join(_DLQ_ROUTE_MARKERS)} — see "
            f"omnimarket.projection.dlq.route_to_dlq, OMN-13548)"
        )
    return violations


def main() -> int:
    repo_root = pathlib.Path.cwd()
    handlers_dir = repo_root / "src" / "omnimarket" / "nodes"
    if not handlers_dir.is_dir():
        print(
            "ERROR: run from repo root (src/omnimarket/nodes not found)",
            file=sys.stderr,
        )
        return 2

    violations = _scan(repo_root)
    if violations:
        print(
            "Projection-DLQ gate FAILED — a validating projection handler must "
            "route malformed events to a contract-declared DLQ topic, not drop "
            "them silently (OMN-13548 / D-03):",
            file=sys.stderr,
        )
        for v in violations:
            print(f"  - {v}", file=sys.stderr)
        return 1

    print("Projection-DLQ gate OK: all validating projection handlers route to DLQ.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
