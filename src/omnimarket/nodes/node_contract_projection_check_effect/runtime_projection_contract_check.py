# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""EFFECT boundary of the projection contract checks: hook and CI entrypoint (OMN-20567).

The only module of the check that reads the filesystem. It sits beside the other
validator entrypoints, not inside the node package, because the repo's node-purity
gate covers every file under a ``node_*_compute`` directory and has no runtime
exemption (adding one would be a new allowlist). It gathers contract and
handler text, hands explicit text to ``HandlerProjectionContractCheck`` and
prints the verdict. Run from the repository root.

    python -m omnimarket.nodes.node_contract_projection_check_effect.runtime_projection_contract_check --rule access
    python -m ... --rule dlq [handler.py ...]
    python -m ... --rule cursor [--write-baseline [--allow-growth]]

``--report-json PATH`` writes the canonical OMN-2362 report. Exit 0 only on PASS;
a full-tree run that gathered nothing is an ERROR finding and exits non-zero.
"""

import argparse
import sys
from pathlib import Path
from typing import Final, TextIO

from omnibase_core.models.validation.model_validation_report import (
    ModelValidationReport,
)

from omnimarket.nodes.node_contract_projection_check_compute.handlers.check_cursor import (
    missing_cursor_ids,
    tracked_exposures,
)
from omnimarket.nodes.node_contract_projection_check_compute.handlers.findings import (
    RULE_ACCESS,
    RULE_CURSOR_MEMBERSHIP,
    RULE_CURSOR_MISSING,
    RULE_CURSOR_SHRINKABLE,
    RULE_DLQ,
)
from omnimarket.nodes.node_contract_projection_check_compute.handlers.handler_projection_contract_check import (
    HandlerProjectionContractCheck,
)
from omnimarket.nodes.node_contract_projection_check_compute.models import (
    EnumProjectionContractRule,
    ModelProjectionNodeSources,
)
from omnimarket.nodes.node_contract_projection_check_effect.handlers.handler_contract_projection_gather import (
    HandlerContractProjectionGather,
)
from omnimarket.nodes.node_contract_projection_check_effect.models import (
    ModelContractProjectionGatherRequest,
)

__all__ = ["main"]

_NODES_DIR: Final[str] = "src/omnimarket/nodes"
_DEFAULT_BASELINE: Final[str] = "scripts/validation/projection_cursor_baseline.txt"


def _emit(text: str, file: TextIO | None = None) -> None:
    (file or sys.stdout).write(text + "\n")


def _messages(report: ModelValidationReport, rule_id: str) -> list[str]:
    return [f.message for f in report.findings if f.rule_id == rule_id]


def _errors(report: ModelValidationReport) -> list[str]:
    return [f.message for f in report.findings if f.severity == "ERROR"]


def _emit_errors(report: ModelValidationReport) -> int | None:
    errors = _errors(report)
    if not errors:
        return None
    for message in errors:
        _emit(f"ERROR: {message}", file=sys.stderr)
    return 1


def _run_access(root: Path, report: ModelValidationReport) -> int:
    failing = _emit_errors(report)
    if failing is not None:
        return failing
    violations = _messages(report, RULE_ACCESS)
    if violations:
        _emit(
            "Projection contract-access gate FAILED (OMN-16690) — a db_io "
            "contract declares an access capability narrower than the one its "
            "handler uses. The runtime enforces the declaration fail-closed, so "
            "every event on these paths is refused and quarantined while the "
            "caller still sees a 202:",
            file=sys.stderr,
        )
        for message in violations:
            _emit(f"  - {message}", file=sys.stderr)
        _emit(
            "\nFix the CONTRACT (declare what the handler does) — never weaken "
            "the runtime guard.",
            file=sys.stderr,
        )
        return 1
    _emit(
        "Projection contract-access gate OK: every db_io declaration covers its "
        "handler's operations."
    )
    return 0


def _run_dlq(report: ModelValidationReport) -> int:
    failing = _emit_errors(report)
    if failing is not None:
        return failing
    violations = _messages(report, RULE_DLQ)
    if violations:
        _emit(
            "Projection-DLQ gate FAILED — a validating projection handler must "
            "route malformed events to a contract-declared DLQ topic, not drop "
            "them silently (OMN-13548 / D-03):",
            file=sys.stderr,
        )
        for message in violations:
            _emit(f"  - {message}", file=sys.stderr)
        return 1
    _emit("Projection-DLQ gate OK: all validating projection handlers route to DLQ.")
    return 0


def _write_baseline(
    root: Path,
    baseline_path: Path,
    nodes: list[ModelProjectionNodeSources],
    baseline: list[str],
    allow_growth: bool,
) -> int:
    tracked, _ = tracked_exposures(nodes)
    current = missing_cursor_ids(tracked)
    added = sorted(set(current) - set(baseline))
    bootstrapping = not baseline_path.exists()
    if added and not allow_growth and not bootstrapping:
        _emit(
            "REFUSING to write a baseline that grows.\n"
            "These exposures are NEW violations, not pre-existing ones:\n"
            + "".join(f"  + {item}\n" for item in added)
            + "Declare a cursor_column instead, or pass --allow-growth with a reason.",
            file=sys.stderr,
        )
        return 1
    header = (
        "# OMN-18043 — projection_api exposures that declare no cursor_column.\n"
        "# This list may only SHRINK. Regenerate with:\n"
        "#   uv run python -m omnimarket.nodes.node_contract_projection_check_effect.runtime_projection_contract_check"
        " --rule cursor --write-baseline\n"
    )
    baseline_path.write_text(header + "".join(f"{item}\n" for item in current))
    _emit(f"baseline written: {len(current)} exposures")
    return 0


def _run_cursor(
    report: ModelValidationReport,
    root: Path,
    baseline_path: Path,
    nodes: list[ModelProjectionNodeSources],
    baseline: list[str],
    write_baseline: bool,
    allow_growth: bool,
) -> int:
    for finding in report.findings:
        if finding.severity == "WARN" and finding.rule_id not in {
            RULE_CURSOR_SHRINKABLE
        }:
            _emit(f"warning: {finding.message}", file=sys.stderr)
    errors = _emit_errors(report)
    if errors is not None:
        return errors
    hard = _messages(report, RULE_CURSOR_MEMBERSHIP)
    new = _messages(report, RULE_CURSOR_MISSING)
    fixed = _messages(report, RULE_CURSOR_SHRINKABLE)

    if hard:
        _emit(
            f"FAIL: {len(hard)} projection_api exposure(s) declare a cursor_column that "
            "is not among their declared columns:\n"
            + "".join(f"  {line}\n" for line in hard)
            + "\nThis is never baselined. Add the column to `columns`, or declare the "
            "cursor_column the exposure actually selects.",
            file=sys.stderr,
        )
        if write_baseline:
            _emit(
                "REFUSING to write a baseline while a cursor_column is absent from its "
                "declared columns (--allow-growth does not apply).",
                file=sys.stderr,
            )
            return 1

    if write_baseline:
        return _write_baseline(root, baseline_path, nodes, baseline, allow_growth)

    if new:
        _emit(
            f"FAIL: {len(new)} projection_api exposure(s) declare no cursor_column and are "
            "not in the baseline:\n"
            + "".join(f"  {item}\n" for item in new)
            + "\nWithout a cursor_column the serving path cannot populate next_cursor "
            "(api_server.py gates on `cfg.cursor_column is not None`), so a truncated "
            "page reports next_cursor: null and a caller cannot tell it is truncated.",
            file=sys.stderr,
        )
        return 1

    if hard:
        return 1

    if fixed:
        _emit(
            f"{len(fixed)} exposure(s) now declare a cursor_column and can leave the "
            "baseline — run --write-baseline to shrink it:\n"
            + "".join(f"  {item}\n" for item in fixed)
        )

    tracked, _ = tracked_exposures(nodes)
    current = missing_cursor_ids(tracked)
    _emit(f"OK: {len(current)} exposure(s) without a cursor_column, all baselined.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="projection-contract-check",
        description="Projection contract checks via the canonical COMPUTE node (OMN-20567).",
    )
    parser.add_argument(
        "--rule", required=True, choices=[r.value for r in EnumProjectionContractRule]
    )
    parser.add_argument(
        "filenames", nargs="*", help="dlq rule: explicit handler paths."
    )
    parser.add_argument("--baseline", default=_DEFAULT_BASELINE)
    parser.add_argument("--write-baseline", action="store_true")
    parser.add_argument("--allow-growth", action="store_true")
    parser.add_argument("--report-json", default=None)
    parsed = parser.parse_args(argv)

    root = Path.cwd()
    rule = EnumProjectionContractRule(parsed.rule)
    if (
        rule is not EnumProjectionContractRule.CURSOR
        and not (root / _NODES_DIR).is_dir()
    ):
        _emit(
            "ERROR: run from repo root (src/omnimarket/nodes not found)",
            file=sys.stderr,
        )
        return 2

    baseline_path = root / parsed.baseline
    check_input = HandlerContractProjectionGather().handle(
        ModelContractProjectionGatherRequest(
            root=str(root),
            rule=rule.value,
            filenames=tuple(parsed.filenames),
            baseline_path=parsed.baseline,
        )
    )
    nodes = list(check_input.nodes)
    baseline = list(check_input.cursor_baseline)
    report = HandlerProjectionContractCheck().handle(check_input)
    if parsed.report_json:
        Path(parsed.report_json).write_text(report.model_dump_json(indent=2) + "\n")

    if rule is EnumProjectionContractRule.ACCESS:
        return _run_access(root, report)
    if rule is EnumProjectionContractRule.DLQ:
        return _run_dlq(report)
    return _run_cursor(
        report,
        root,
        baseline_path,
        nodes,
        baseline,
        parsed.write_baseline,
        parsed.allow_growth,
    )


if __name__ == "__main__":
    sys.exit(main())
