# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared helpers of the friction rollup tick tests: the recorded fixtures, a runner and the parity comparison."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from omnimarket.nodes.node_friction_rollup_tick_effect.handlers.handler_friction_rollup_tick import (
    HandlerFrictionRollupTick,
)
from omnimarket.nodes.node_friction_rollup_tick_effect.models.model_friction_rollup_tick import (
    ModelFrictionRollupTickRequest,
    ModelFrictionRollupTickResult,
)
from omnimarket.nodes.node_friction_rollup_tick_effect.protocols.protocol_ledger_appender import (
    InMemoryLedgerAppender,
)

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = ROOT / "tests/fixtures/friction_rollup_tick"
PARITY = json.loads(
    (ROOT / "tests/fixtures/friction_rollup_tick_parity.json").read_text(
        encoding="utf-8"
    )
)
STATE_MARK = "<STATE>"
#: Totals keys the compute node's report carries and the timer's rollup (the unit's main branch) did not: they were
#: added by OMN-22563 (class rules). On the recorded rows no rule matches, so each is 0; the tests assert that.
ADDITIVE_TOTALS_KEYS = ("rows_classified_by_rule",)


def without_additive(json_text: str) -> str:
    """The report JSON with the additive totals keys removed (asserting each is 0), formatted as the tick writes it."""
    document = json.loads(json_text)
    for window in document["windows"].values():
        for key in ADDITIVE_TOTALS_KEYS:
            assert window["totals"].pop(key) == 0
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


def install_ledger(tmp_path: Path, *, archive: bool = True) -> Path:
    """Copy the recorded ledger (and its archive) under tmp_path and return the ledger file."""
    ledger_dir = tmp_path / "ledger"
    ledger_dir.mkdir(parents=True, exist_ok=True)
    ledger = ledger_dir / "ledger.md"
    shutil.copy(FIXTURES / "ledger_real_rows.md", ledger)
    if archive:
        shutil.copytree(
            FIXTURES / "archive", ledger_dir / "archive", dirs_exist_ok=True
        )
    return ledger


def request_for(
    tmp_path: Path, ledger: Path, now: str | None, *, dry_run: bool = False
) -> ModelFrictionRollupTickRequest:
    return ModelFrictionRollupTickRequest(
        registry_root=str(tmp_path / "registry"),
        ledger_path=str(ledger),
        now=now,
        dry_run=dry_run,
        state_dir=str(tmp_path / "state"),
    )


def run_case(
    tmp_path: Path, case: dict[str, Any]
) -> list[tuple[dict[str, Any], ModelFrictionRollupTickResult]]:
    """Run every recorded tick of a case through the node, in order, against one state directory."""
    ledger = install_ledger(tmp_path, archive=bool(case["archive"]))
    handler = HandlerFrictionRollupTick(appender=InMemoryLedgerAppender())
    return [
        (
            tick,
            handler.handle(
                request_for(
                    tmp_path, ledger, tick["now"], dry_run=bool(tick["dry_run"])
                )
            ),
        )
        for tick in case["ticks"]
    ]


def observed(result: ModelFrictionRollupTickResult, state_dir: Path) -> dict[str, Any]:
    """The node's output in the shape the timer's recorded tick has."""
    stdout = result.log_line.replace(str(state_dir), STATE_MARK) + "\n"
    if result.state == "DRY-RUN":
        stdout = result.status_row + "\n" + stdout
    return {
        "exit_code": result.exit_code,
        "stdout": stdout,
        "row": result.status_row,
        "json_text": without_additive(result.report_json_text),
        "markdown_text": result.report_markdown_text,
    }


def tick_diff(expected: dict[str, Any], actual: dict[str, Any]) -> list[str]:
    """Every field of the recorded tick the node's output does not equal, with its first differing line."""
    differences: list[str] = []
    for field in ("exit_code", "stdout", "row", "json_text", "markdown_text"):
        if field not in expected:
            continue
        want, got = expected[field], actual[field]
        if want == got:
            continue
        if isinstance(want, str) and isinstance(got, str):
            want_lines, got_lines = want.splitlines(), got.splitlines()
            at = next(
                (
                    i
                    for i, pair in enumerate(zip(want_lines, got_lines, strict=False))
                    if pair[0] != pair[1]
                ),
                min(len(want_lines), len(got_lines)),
            )
            differences.append(
                f"{field}: line {at + 1}: timer={want_lines[at : at + 1]!r} node={got_lines[at : at + 1]!r}"
            )
        else:
            differences.append(f"{field}: timer={want!r} node={got!r}")
    return differences
