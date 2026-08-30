#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract-vs-handler publish parity gate (OMN-17017, A10 step 1).

A node contract that declares ``event_bus.publish_topics`` its own Python never
publishes is a machine-readable lie: the CLI registry, the skill docs, and every
audit downstream read the declaration as the system. ``node_wave_scheduler_
orchestrator`` declared five publish topics — including
``wave-scheduler-stall-detected.v1`` and
``wave-scheduler-dependency-violation.v1`` — while its handler contained zero
``publish`` / ``Envelope`` / ``event_bus`` references (2026-08-29 beta
off-the-rails analysis rev 2, §RC-J).

Rule
----
For every ``src/omnimarket/nodes/*/contract.yaml``: each declared publish topic
that is NOT the contract's ``terminal_event`` requires evidence of a publish or
emit call somewhere in that node package's Python. ``terminal_event`` is exempt
because the runtime publishes it on the handler's behalf.

The gate is a burn-down ratchet against
``scripts/ci/contract_publish_parity_baseline.py``: the frozen set may only
shrink. A new offender hard-fails.

Exit codes:
    0: no new offenders
    1: at least one node outside the frozen baseline declares an unpublished topic
"""

from __future__ import annotations

import argparse
import importlib.util
import re
import sys
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

# Evidence that a node's Python actually reaches the bus. Deliberately broad:
# the gate's job is to catch contracts with ZERO publish surface, not to police
# which publish helper a node chose.
_PUBLISH_EVIDENCE = re.compile(
    r"\bpublish\w*\s*\(|\bevent_bus\b|ModelEventEnvelope|\.emit\s*\(|\bemit_event\b"
)


class ModelPublishParityFinding(BaseModel):
    """One node whose contract declares topics its Python never publishes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    node: str = Field(description="Node package directory name.")
    undeclared_topics: tuple[str, ...] = Field(
        description="Sorted publish topics with no matching publish/emit call."
    )


def nodes_root(repo_root: Path) -> Path:
    """Directory holding one subdirectory per node package."""
    return repo_root / "src" / "omnimarket" / "nodes"


def scan_publish_parity(root: Path) -> list[ModelPublishParityFinding]:
    """Return one finding per node that declares an unpublished topic."""
    findings: list[ModelPublishParityFinding] = []
    for contract_path in sorted(root.glob("*/contract.yaml")):
        raw = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            continue
        event_bus = raw.get("event_bus") or {}
        if not isinstance(event_bus, dict):
            continue
        declared = event_bus.get("publish_topics") or []
        if not isinstance(declared, list):
            continue
        terminal_event = raw.get("terminal_event")
        candidates = sorted(
            {str(topic) for topic in declared if str(topic) != str(terminal_event)}
        )
        if not candidates:
            continue
        node_dir = contract_path.parent
        if _has_publish_evidence(node_dir):
            continue
        findings.append(
            ModelPublishParityFinding(
                node=node_dir.name,
                undeclared_topics=tuple(candidates),
            )
        )
    return findings


def _has_publish_evidence(node_dir: Path) -> bool:
    for python_path in sorted(node_dir.rglob("*.py")):
        if _PUBLISH_EVIDENCE.search(python_path.read_text(encoding="utf-8")):
            return True
    return False


def _repo_root() -> Path:
    candidate = Path(__file__).resolve().parent
    while candidate != candidate.parent:
        if (candidate / ".git").exists():
            return candidate
        candidate = candidate.parent
    return Path.cwd()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--update",
        action="store_true",
        help="rewrite the frozen baseline from the current tree (never in CI).",
    )
    parser.add_argument(
        "paths",
        nargs="*",
        help="ignored; accepted so the check can run as a pre-commit files hook.",
    )
    args = parser.parse_args(argv)

    repo_root = _repo_root()
    findings = scan_publish_parity(nodes_root(repo_root))
    baseline_path = repo_root / "scripts" / "ci" / "contract_publish_parity_baseline.py"

    if args.update:
        _write_baseline(baseline_path, tuple(sorted({f.node for f in findings})))
        print(f"wrote {len(findings)} entries to {baseline_path}")
        return 0

    frozen = set(_load_baseline(baseline_path))

    offenders = [f for f in findings if f.node not in frozen]
    stale = sorted(frozen - {f.node for f in findings})

    for finding in offenders:
        print(
            f"FAIL {finding.node}: contract declares publish topics with no "
            f"publish/emit call in the node: {', '.join(finding.undeclared_topics)}"
        )
    for node in stale:
        print(
            f"FAIL {node}: repaired but still frozen in PARITY_DEBT — remove it "
            "(the baseline is a burn-down list, not an allowlist)."
        )

    if offenders or stale:
        return 1
    print(f"contract publish parity OK ({len(frozen)} frozen debt entries)")
    return 0


def _load_baseline(path: Path) -> tuple[str, ...]:
    """Load the frozen baseline by path so the check runs as a bare script too."""
    spec = importlib.util.spec_from_file_location(
        "contract_publish_parity_baseline", path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load publish-parity baseline from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    debt: tuple[str, ...] = module.PARITY_DEBT
    return debt


def _write_baseline(path: Path, nodes: tuple[str, ...]) -> None:
    body = "\n".join(f'    "{node}",' for node in nodes)
    path.write_text(
        "# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.\n"
        "# SPDX-License-Identifier: MIT\n"
        '"""FROZEN contract-vs-handler publish parity debt — OMN-17017.\n'
        "\n"
        "Generated by ``scripts/ci/check_contract_publish_parity.py --update``.\n"
        "\n"
        "``PARITY_DEBT`` is the frozen set of nodes whose ``contract.yaml`` declares a\n"
        "publish topic (other than ``terminal_event``) that the node's Python never\n"
        "publishes. It is monotonically NON-INCREASING: it may only shrink. A NEW\n"
        "offender, or growth of this set, HARD-FAILS CI + pre-commit. A node leaves\n"
        "the set by publishing the event or by deleting the declaration.\n"
        '"""\n'
        "\n"
        "PARITY_DEBT: tuple[str, ...] = (\n"
        f"{body}\n"
        ")\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    sys.exit(main())
