# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Transport adapters to the core-owned relay rule, shared by both writers.

Selection and row projection live here; the validation predicate lives in core.
An unavailable validator refuses selected relays, never silently admits them.
"""

from importlib import import_module, resources

import yaml

from omnimarket.models.model_relay_admission import (
    ModelRelayAdmissionResult,
    ModelRelayRequiresRule,
    ModelRelayRow,
)
from omnimarket.protocols.protocol_relay_admission import ProtocolRelayAdmission


def load_relay_requires_rule() -> ModelRelayRequiresRule:
    return ModelRelayRequiresRule.model_validate(
        yaml.safe_load(
            resources.files("omnimarket.nodes.node_onex_ledger_admission_compute")
            .joinpath("relay_requires.yaml")
            .read_text()
        )
    )


def relay_rows(payload: str) -> tuple[ModelRelayRow, ...]:
    rows: list[ModelRelayRow] = []
    for line in payload.splitlines():
        # Indented continuations are text, not independently appended rows.
        if line[:1].isspace():
            continue
        parts = [part.strip() for part in line.split("|")]
        if len(parts) < 3:
            continue
        cells: dict[str, tuple[str, ...]] = {}
        for part in parts[2:]:
            key, sep, value = part.partition("=")
            if sep:
                cells[key] = (*cells.get(key, ()), value)
        rows.append(ModelRelayRow(row_type=parts[1], cells=cells))
    return tuple(rows)


def relay_argv_rows(
    argv: list[str], append_rows_text: str | None
) -> tuple[ModelRelayRow, ...]:
    if argv[:1] == ["append-rows"]:
        return relay_rows(append_rows_text or "")
    if "--append" in argv[:-1]:
        return relay_rows(argv[argv.index("--append") + 1])
    if len(argv) < 2 or argv[0] != "row":
        return ()
    cells: dict[str, tuple[str, ...]] = {}
    i = 2
    while i < len(argv):
        flag, eq, inline = argv[i].partition("=")
        if not flag.startswith("--"):
            i += 1
            continue
        value = inline if eq else (argv[i + 1] if i + 1 < len(argv) else "")
        key = flag[2:]
        if key == "kv":
            key, _, value = value.partition("=")
        cells[key] = (*cells.get(key, ()), value)
        i += 1 if eq else 2
    return (ModelRelayRow(row_type=argv[1], cells=cells),)


class CoreRelayAdmission:
    """JSON seam: core never imports Market types or depends on this package."""

    def evaluate(
        self, rule: ModelRelayRequiresRule, rows: tuple[ModelRelayRow, ...]
    ) -> ModelRelayAdmissionResult:
        try:
            evaluate = getattr(
                import_module(rule.evaluator_module), rule.evaluator_function
            )
            return ModelRelayAdmissionResult.model_validate(
                evaluate(
                    rule=rule.model_dump(mode="json"),
                    rows=[row.model_dump(mode="json") for row in rows],
                )
            )
        except (ImportError, AttributeError, ValueError, TypeError) as exc:
            return ModelRelayAdmissionResult(
                refusals=(
                    f"{rule.rule_id}: core evaluator unavailable ({type(exc).__name__}); {rule.fix}",
                )
            )


def relay_admission_refusals(
    rows: tuple[ModelRelayRow, ...],
    evaluator: ProtocolRelayAdmission,
    rule: ModelRelayRequiresRule,
) -> tuple[str, ...]:
    selected = tuple(
        row
        for row in rows
        if row.row_type == rule.row_type and rule.trigger_cell in row.cells
    )
    if not selected:
        return ()
    return evaluator.evaluate(rule, selected).refusals
