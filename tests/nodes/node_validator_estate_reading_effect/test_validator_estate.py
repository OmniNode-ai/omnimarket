# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error tests of the validator estate reading (OMN-23080).

The golden test reads a small synthetic estate and asserts every number of the row. The error tests inject a
named fault (an unreadable core repository, a refused append) and assert the typed failure. The named break
(``NAMED_MUTATIONS``) plants a rule table in a canonical validator and shows the canonical count falling, so the
count is shown able to fail. The last test draws the row through the floor's own shadow reader.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import pytest
import yaml

from omnimarket.nodes.node_floor_simple_effect.handlers.ledger_reads import (
    shadow_readings,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
    evidence_row_from_text,
)
from omnimarket.nodes.node_validator_estate_reading_effect.handlers.handler_validator_estate import (
    HandlerValidatorEstate,
)
from omnimarket.nodes.node_validator_estate_reading_effect.models.model_validator_estate import (
    ModelValidatorEstateCommand,
    ModelValidatorEstateResult,
)
from omnimarket.nodes.node_validator_estate_reading_effect.overlay import (
    ModelEstateOverlay,
)
from omnimarket.nodes.node_validator_estate_reading_effect.protocols import (
    EstateSourceError,
)

pytestmark = pytest.mark.unit

CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_validator_estate_reading_effect/contract.yaml"
)

NOW = datetime(2026, 11, 8, 4, 30, tzinfo=UTC)
HOST = "host-a"

#: The break each assertion is shown red under.
NAMED_MUTATIONS = {
    "planted_rule_table": "a rule table written in a canonical validator's module lowers the canonical count by one",
    "core_unreadable": "the core repository cannot be read: the row carries no numbers",
    "append_refused": "the ledger refuses the row: the run fails",
}

OVERLAY = {
    "repos_root_env": "EXAMPLE_CLONES_ROOT",
    "core_repo": "core",
    "repos": [
        {
            "name": "core",
            "ref": "main",
            "source_roots": ["src/core"],
            "hook_config": ".pre-commit-config.yaml",
        },
        {
            "name": "app",
            "ref": "main",
            "source_roots": ["src/app"],
            "hook_config": ".pre-commit-config.yaml",
        },
    ],
    "generic_format": {
        "base_class": "ValidatorBase",
        "base_module_path": "src/core/validation/validator_base.py",
        "contract_subdir": "contracts",
        "contract_suffix": ".validation.yaml",
        "rules_key": "rules",
        "nested_key": "validation",
        "literal_table_min_strings": 3,
        "literal_pattern_calls": ["compile"],
        "bus_trace_names": ["HandlerTraceEmit"],
        "bespoke_suffixes": ["Validator", "Checker"],
    },
    "hooks": {
        "local_repo_value": "local",
        "own_repo_fragments": ["example-org"],
        "pytest_marker": "pytest",
    },
    "plan": {
        "repo": "kb",
        "root_env": "EXAMPLE_KB_ROOT",
        "ref": "main",
        "path": "plan.md",
        "task_pattern": r"^(?:- )?\*\*(T-?[A-Z]?\d(?:[\w.]*\w)?)",
        "ticket_pattern": r"EX-\d+",
        "minted_pattern": r"(T\d+\.\d+[a-z]?) ((?:EX-\d+)(?: (?:and|to) EX-\d+)*)",
        "exclude_tickets": ["EX-1"],
        "merge_repos": ["core", "app"],
    },
    "ledger_append": {
        "command_env": "EXAMPLE_APPEND",
        "host_env": "EXAMPLE_HOST",
        "timeout_seconds": 5,
    },
    "publish": {
        "lane": "validator-estate",
        "actor": "node:example",
        "publish_max_age_seconds": 3600,
    },
    "git_timeout_seconds": 5,
    "injected_fault": "the core repository cannot be read",
}

BASE = "class ValidatorBase:\n    pass\n"
GOOD = (
    "from core.validation.validator_base import ValidatorBase\n\n"
    "class ValidatorAlpha(ValidatorBase):\n    validator_id = 'alpha'\n"
)
TABLE = "NAMES = ('a', 'b', 'c')\n"
RULES = "rules:\n  - id: r1\n"
PLAN = (
    "- **Ticket:** EX-1, minted: T0.1 EX-10, T0.2 EX-11 and EX-12, T0.3 EX-20 to EX-22\n\n"
    "**T0.1 First task (UPDATE).**\n- Depends on: nothing.\n\n"
    "- **T0.2 Second (EX-11).** falsifier: EX-99\n"
    "- **T0.3 Third (NEW ticket).**\n"
    "- **T0.4 Fourth (NEW ticket).**\n"
    "- **T0.5 Fifth (EX-30, EX-31).**\n"
)


class InMemorySource:
    def __init__(
        self, files: dict[tuple[str, str], str], cites: dict[str, int]
    ) -> None:
        self.files = files
        self.cites = cites
        self.unreadable: set[str] = set()

    def head(self, repo: str, ref: str) -> str:
        if repo in self.unreadable:
            raise EstateSourceError(f"{repo}: ref {ref} not readable")
        return "a" * 40

    def files_containing(
        self, repo: str, ref: str, pattern: str, roots: tuple[str, ...]
    ) -> tuple[str, ...]:
        import re

        return tuple(
            path
            for (r, path), text in sorted(self.files.items())
            if r == repo
            and path.endswith(".py")
            and any(path.startswith(root) for root in roots)
            and re.search(pattern, text)
        )

    def read(self, repo: str, ref: str, path: str) -> str | None:
        return self.files.get((repo, path))

    def commits_citing(self, repo: str, ref: str, ticket: str) -> int:
        return self.cites.get(ticket, 0)


def estate(extra_table: bool = False, base_emits: bool = False) -> InMemorySource:
    files = {
        ("core", "src/core/validation/validator_base.py"): BASE
        + ("# HandlerTraceEmit\n" if base_emits else ""),
        ("core", "src/core/validation/validator_alpha.py"): GOOD
        + (TABLE if extra_table else ""),
        ("core", "src/core/validation/contracts/alpha.validation.yaml"): RULES,
        # no rule document: the rules are in code
        ("core", "src/core/validation/validator_beta.py"): GOOD.replace(
            "alpha", "beta"
        ).replace("Alpha", "Beta"),
        # a bespoke validator outside the format
        ("app", "src/app/checks/thing_checker.py"): "class ThingChecker:\n    pass\n",
        ("core", ".pre-commit-config.yaml"): (
            "repos:\n- repo: local\n  hooks:\n"
            "  - {id: alpha-gate, entry: python -m core.validation.validator_alpha}\n"
            "  - {id: tests, entry: uv run pytest tests/}\n"
            "- repo: https://example.com/third-party/tool\n  hooks:\n  - {id: tool}\n"
        ),
        ("app", ".pre-commit-config.yaml"): (
            "repos:\n- repo: local\n  hooks:\n  - {id: script-gate, entry: scripts/check.sh}\n"
        ),
        ("kb", "plan.md"): PLAN,
    }
    return InMemorySource(
        files, {"EX-10": 1, "EX-20": 1, "EX-21": 2, "EX-22": 1, "EX-99": 5, "EX-30": 1}
    )


def handler(
    source: InMemorySource, ledger: InMemoryLedger | None = None
) -> HandlerValidatorEstate:
    return HandlerValidatorEstate(
        overlay=ModelEstateOverlay.model_validate(OVERLAY),
        host=HOST,
        source=source,
        appender=ledger,
        clock=lambda: NOW,
    )


def run(
    source: InMemorySource,
    ledger: InMemoryLedger | None = None,
    fault: Literal["none", "core-unreadable"] = "none",
) -> ModelValidatorEstateResult:
    return handler(source, ledger).handle(
        ModelValidatorEstateCommand(
            correlation_id="c-1",
            write=ledger is not None,
            fault=fault,
        )
    )


def test_golden_estate_numbers_and_row() -> None:
    ledger = InMemoryLedger()
    result = run(estate(), ledger)
    assert result.outcome == "read"
    assert result.status == "completed"
    # alpha is canonical; beta has no rule document; one bespoke checker is outside the format.
    assert [c.name for c in result.classes if c.canonical] == ["ValidatorAlpha"]
    assert result.other_classes == 2
    # three own hooks in core and one in app; the third-party hook is not ours; the alpha hook runs a counted class.
    assert [(h.repo, h.hooks, h.covered, h.pytest) for h in result.hooks] == [
        ("core", 2, 1, 1),
        ("app", 1, 0, 0),
    ]
    assert result.other_hooks == 2
    assert (result.canonical, result.estate) == (1, 5)
    assert result.canonical_emitting == 0
    states = {t.task: t.state for t in result.tasks}
    assert states == {
        "T0.1": "merged",
        "T0.2": "open",
        "T0.3": "merged",
        "T0.4": "no-ticket",
        "T0.5": "partial",
    }
    assert result.ledger == "written"
    row = evidence_row_from_text(ledger.lines[0])
    assert row is not None
    assert row.row_type == "STATUS"
    cells = row.cells
    assert cells["src"] == "validator-estate"
    assert cells["drill"] == "no"
    assert cells["outcome"] == "read"
    assert cells["canonical_of"] == "1/5"
    assert cells["traces_of"] == "0/1"
    assert cells["tasks_merged_of"] == "2/5"
    assert cells["tasks_partial"] == "1"
    assert cells["tasks_open"] == "1"
    assert cells["tasks_no_ticket"] == "1"
    assert cells["max_age"] == "3600"


def test_contract_terminal_event_is_the_declared_completed_topic() -> None:
    contract = yaml.safe_load(CONTRACT.read_text())
    completed = "onex.evt.omnimarket.validator-estate-run-completed.v1"
    failed = "onex.evt.omnimarket.validator-estate-run-failed.v1"
    assert (
        contract["terminal_event"]
        == "onex.evt.omnimarket.validator-estate-run-completed.v1"
    )
    assert contract["topics"]["validator_estate_run_completed"] == completed
    assert contract["topics"]["validator_estate_run_failed"] == failed
    assert sorted(contract["externally_consumed_topics"]) == sorted([completed, failed])


def test_a_ticket_in_the_plans_falsifier_text_is_not_the_tasks_ticket() -> None:
    result = run(estate())
    t02 = next(t for t in result.tasks if t.task == "T0.2")
    assert t02.tickets == (
        "EX-11",
        "EX-12",
    )  # EX-99 sits in non-bold text and is cited by commits, yet T0.2 stays open


def test_trace_share_reads_the_base_and_the_module() -> None:
    assert run(estate(base_emits=True)).canonical_emitting == 1


def test_named_break_planted_rule_table_lowers_the_canonical_count() -> None:
    before = run(estate())
    after = run(estate(extra_table=True))
    assert (before.canonical, after.canonical) == (1, 0)
    assert after.classes[0].literal_tables == 1
    assert after.estate == before.estate


def test_error_core_unreadable_is_not_an_empty_estate() -> None:
    ledger = InMemoryLedger()
    result = run(estate(), ledger, fault="core-unreadable")
    assert result.outcome == "source-unreadable"
    assert result.classes == ()
    row = evidence_row_from_text(ledger.lines[0])
    assert row is not None
    cells = row.cells
    assert cells["outcome"] == "source-unreadable"
    assert cells["drill"] == "yes"
    assert "canonical_of" not in cells


def test_error_unreadable_repository_is_typed() -> None:
    source = estate()
    source.unreadable.add("app")
    result = run(source)
    assert result.outcome == "source-unreadable"
    assert "app" in result.unreadable[0]


def test_error_append_refused_fails_the_run() -> None:
    ledger = InMemoryLedger()
    ledger.append_status = "refused"
    result = run(estate(), ledger)
    assert result.status == "failed"
    assert result.ledger == "append-failed:refused"


def test_error_writing_run_without_appender_fails() -> None:
    result = handler(estate()).handle(
        ModelValidatorEstateCommand(correlation_id="c", write=True)
    )
    assert result.status == "failed"
    assert result.failure == "no-appender-for-a-writing-run"


def test_floor_shadow_reader_draws_the_row() -> None:
    from omnimarket.models.floor_17a.model_floor_17a import ModelLedgerShadowRule

    ledger = InMemoryLedger()
    run(estate(), ledger)
    rule = ModelLedgerShadowRule.model_validate(
        {
            "piece": 4,
            "label": "Validators",
            "row_type": "STATUS",
            "lane": "validator-estate",
            "cells": {"src": "validator-estate", "drill": "no"},
            "published_at_cell": "published_at",
            "max_age_cell": "max_age",
            "fields": [
                {"cell": "canonical_of", "label": "canonical generic"},
                {"cell": "traces_of", "label": "with bus traces"},
                {"cell": "tasks_merged_of", "label": "plan tasks merged"},
            ],
            "outcome_cell": "outcome",
            "outcome_ok": "read",
            "lookback_days": 3,
        }
    )
    rows = [
        r for line in ledger.lines if (r := evidence_row_from_text(line)) is not None
    ]
    reading, rejected = shadow_readings(rows, rule)
    assert rejected == 0
    assert reading is not None
    assert reading.failed_outcome is None
    assert reading.values == (
        ("canonical generic", "1/5"),
        ("with bus traces", "0/1"),
        ("plan tasks merged", "2/5"),
    )
