# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Two routes may not claim one local-ingress alias (OMN-17888).

WHY THIS EXISTS, AND WHY BOOT WAS THE WRONG PLACE TO FIND IT
------------------------------------------------------------
``omnibase_infra`` ``runtime_local_ingress.py`` registers one local-ingress alias per
``handler_routing`` entry, keyed on that entry's ``operation``, and refuses a second
registration of the same alias unless the two routes expose the SAME interface
(``_local_ingress_routes_equivalent`` compares, among other fields, the input model). On a
collision it raises::

    ValueError: Duplicate local ingress route alias '<package>.<node>.<operation>'

That raise happens at RUNTIME BOOT. On 2026-09-16 a contract change that was green through
every gate in both repositories made ``node_redeploy_deploy_effect`` declare one operation
twice with two different input models. ``omninode-runtime`` crash-looped, `:8085/ready`
refused, the compose-dev lab-pass receipt FAILed on ``ready_main`` and
``health_dimensions``, and because delivery to staging is fail-closed on that receipt the
dev lane, staging delivery and both compose lanes were down for an afternoon.

Nothing at PR time asked the question. This module asks it.

WHAT IT CHECKS
--------------
1. **The real boot condition, not a re-derivation.** It calls
   ``discover_runtime_local_ingress_routes`` — the function the runtime itself calls at
   boot — and turns the ``ValueError`` it raises into a reported finding. A gate that
   re-implemented the alias rule could drift from the runtime; this one cannot, because it
   IS the runtime's own code.
2. **The contract-level cause, named.** The runtime error names an alias and a file twice;
   it does not say which operation or which models disagree. So the static half also reports
   any operation in one contract that is declared with more than one input model, which is
   the shape that produced the collision, with the contract, the operation and both model
   names.

Both halves run. The first is authoritative about whether the runtime boots; the second is
what tells the author what to change.

FAIL-CLOSED
-----------
A scan that resolves no package root, or that produces an implausibly small route table, is
a broken scan rather than a clean tree and exits 2. An empty result and a collapsed
discovery are otherwise the same output, and a gate that cannot tell them apart reports the
second as the first (rule 16).

Run it directly::

    uv run python -m omnimarket.validators.local_ingress_alias_collision
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "ModelLocalIngressAliasFinding",
    "collect_findings",
    "main",
]

_SCANNED_PACKAGE = "omnimarket"
_CONTRACT_FILENAME = "contract.yaml"

# A route table smaller than this means discovery collapsed rather than that the tree is
# clean. Measured on the tree at 2026-09-16: 403 contracts produce 2,486 aliases.
_MIN_EXPECTED_ALIASES = 500


class ModelLocalIngressAliasFinding(BaseModel):
    """One reason the local-ingress alias table cannot be built."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: str = Field(
        ...,
        description=(
            "'alias_collision' when the runtime's own registry refused the table, "
            "'operation_input_model_conflict' for the contract-level cause."
        ),
    )
    detail: str = Field(..., description="Human-readable statement of the finding.")
    contract: str | None = Field(
        default=None, description="Contract name, when the finding names one."
    )
    operation: str | None = Field(
        default=None, description="Operation, when the finding names one."
    )

    def render(self) -> str:
        """One line naming the kind and the detail."""
        return f"[{self.kind}] {self.detail}"


def _named(value: object) -> str | None:
    """The ``name`` of a contract sub-block written as a mapping or as a bare string."""
    if isinstance(value, dict):
        name = value.get("name")
        return name if isinstance(name, str) else None
    if isinstance(value, str):
        return value
    return None


def _operation_input_model_conflicts(
    scan_root: Path,
) -> tuple[tuple[ModelLocalIngressAliasFinding, ...], int]:
    """Operations declared with more than one input model, contract by contract."""
    findings: list[ModelLocalIngressAliasFinding] = []
    contracts_read = 0
    for contract_path in sorted(scan_root.rglob(_CONTRACT_FILENAME)):
        document = yaml.safe_load(contract_path.read_text())
        if not isinstance(document, dict):
            continue
        contracts_read += 1
        entries = (document.get("handler_routing") or {}).get("handlers")
        if not isinstance(entries, list):
            continue

        by_operation: dict[str, set[str]] = {}
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            operation = entry.get("operation")
            model = _named(entry.get("input_model"))
            if isinstance(operation, str) and operation.strip() and model:
                by_operation.setdefault(operation.strip(), set()).add(model)

        contract_name = document.get("name")
        for operation, models in sorted(by_operation.items()):
            if len(models) < 2:
                continue
            findings.append(
                ModelLocalIngressAliasFinding(
                    kind="operation_input_model_conflict",
                    contract=contract_name if isinstance(contract_name, str) else None,
                    operation=operation,
                    detail=(
                        f"{contract_path}: operation {operation!r} is declared with "
                        f"{len(models)} different input models "
                        f"({', '.join(sorted(models))}). The operation is the "
                        "caller-facing local-ingress alias and resolves to exactly one "
                        "route, whose input model validates the caller's payload, so the "
                        "runtime refuses the second registration at boot. Give each "
                        "entry its own operation."
                    ),
                )
            )
    return tuple(findings), contracts_read


def _alias_table_findings() -> tuple[tuple[ModelLocalIngressAliasFinding, ...], int]:
    """Build the route table the way the runtime does, and report a refusal."""
    from omnibase_infra.runtime.runtime_local_ingress import (
        discover_runtime_local_ingress_routes,
    )

    try:
        routes = discover_runtime_local_ingress_routes([_SCANNED_PACKAGE])
    except ValueError as exc:
        return (
            (
                ModelLocalIngressAliasFinding(
                    kind="alias_collision",
                    detail=(
                        f"the runtime's own local-ingress registry refuses this tree: "
                        f"{exc}. This is the exact error omninode-runtime raises at boot, "
                        "reported here instead."
                    ),
                ),
            ),
            0,
        )
    return (), len(routes)


def collect_findings() -> tuple[tuple[ModelLocalIngressAliasFinding, ...], int, int]:
    """Return findings, the alias count, and the number of contracts read."""
    alias_findings, alias_count = _alias_table_findings()
    scan_root = Path(__file__).resolve().parents[1] / "nodes"
    conflict_findings, contracts_read = _operation_input_model_conflicts(scan_root)
    return (*alias_findings, *conflict_findings), alias_count, contracts_read


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: 1 on a finding, 2 when the scan itself is unusable."""
    if argv:
        sys.stderr.write(
            "[local-ingress-alias-collision] FAIL: this gate takes no arguments; it "
            f"always scans the installed {_SCANNED_PACKAGE} package, exactly as the "
            f"runtime does. Got {argv!r}.\n"
        )
        return 2

    findings, alias_count, contracts_read = collect_findings()

    if findings:
        sys.stderr.write(
            f"[local-ingress-alias-collision] FAIL: {len(findings)} finding(s); the "
            "runtime would not boot:\n"
        )
        for finding in findings:
            sys.stderr.write(f"  - {finding.render()}\n")
        return 1

    if contracts_read == 0:
        sys.stderr.write(
            "[local-ingress-alias-collision] FAIL: no contract.yaml was read; a "
            "zero-contract scan is a broken scan, not a clean tree.\n"
        )
        return 2

    if alias_count < _MIN_EXPECTED_ALIASES:
        sys.stderr.write(
            f"[local-ingress-alias-collision] FAIL: the alias table collapsed to "
            f"{alias_count} entries (expected at least {_MIN_EXPECTED_ALIASES}); a "
            "collapsed discovery cannot report a collision it never built.\n"
        )
        return 2

    sys.stderr.write(
        f"[local-ingress-alias-collision] OK: {alias_count} local-ingress aliases built "
        f"from {contracts_read} contracts, 0 collisions.\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
