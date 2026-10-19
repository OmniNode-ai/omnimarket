# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain for the invariants select compute (OMN-21126): selection, provenance, refusals, purity."""

from __future__ import annotations

import ast
import importlib
import re
import tomllib
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from omnibase_core.enums.enum_context_factor import EnumContextFactor
from omnibase_core.enums.enum_context_source_status import EnumContextSourceStatus
from omnibase_core.enums.enum_context_span_unit import EnumContextSpanUnit
from omnibase_core.enums.enum_context_supersession_status import (
    EnumContextSupersessionStatus,
)

from omnimarket.nodes.node_context_invariants_select_compute.handlers.handler_context_invariants_select import (
    HandlerContextInvariantsSelect,
)
from omnimarket.nodes.node_context_invariants_select_compute.models.model_context_invariants_select import (
    EnumInvariantExclusionReason,
    EnumInvariantsSelectError,
    ModelInvariantsSelectRequest,
    ModelInvariantsSelectResult,
)

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
NODE_NAME = "node_context_invariants_select_compute"
PACKAGE = f"omnimarket.nodes.{NODE_NAME}"
NODE = ROOT / "src/omnimarket/nodes" / NODE_NAME
SHA = "0123456789abcdef0123456789abcdef01234567"
SOURCE_PATH = "reference/invariants.yaml"
AS_OF = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)

REGISTRY_TEXT = """# Synthetic invariant registry for selection tests.
schema_version: 1
scopes:
  - architecture
  - delegation
  - process

invariants:
  # The file order is intentional; active and retired entries are interleaved.
  - id: delegate-explicitly
    statement: >-
      Assign a concrete task
      with a clear completion condition.
    scope: [delegation]
    ruled:
      date: "2026-01-01"
      source: synthetic-ruling-01
    enforcement: >-
      Check the assignment
      before dispatch.
    status: active

  # Architecture entries do not all participate in delegation.
  - id: preserve-boundaries
    statement: >-
      Keep component responsibilities
      explicit at their boundaries.
    scope: [architecture]
    ruled:
      date: "2026-01-02"
      source: synthetic-ruling-02
    enforcement: >-
      Review the declared
      component interfaces.
    status: active

  - id: retired-dispatch
    statement: >-
      Use the retired dispatch
      convention for assignments.
    scope: [delegation]
    ruled:
      date: "2026-01-03"
      source: synthetic-ruling-03
    enforcement: >-
      Retain this ruling
      only for historical context.
    status: superseded
    superseded_by: delegate-explicitly

  - id: delegate-through-interfaces
    statement: >-
      Delegate across components
      through declared interfaces.
    scope: [architecture, delegation]
    ruled:
      date: "2026-01-04"
      source: synthetic-ruling-04
    enforcement: >-
      Check each delegated call
      against its interface.
    status: active

  - id: retired-process
    statement: >-
      Follow the retired review
      sequence for every change.
    scope: [process]
    ruled:
      date: "2026-01-05"
      source: synthetic-ruling-05
    enforcement: >-
      Keep the historical sequence
      available for audit.
    status: superseded
    superseded_by: preserve-boundaries

  # This comment belongs to the list, rather than to a scalar.
  - id: report-delegated-outcomes
    statement: >-
      Report the outcome
      of each delegated task.
    scope: [delegation]
    ruled:
      date: "2026-01-06"
      source: synthetic-ruling-06
    enforcement: >-
      Require an outcome
      before closing the assignment.
    status: active

  - id: declare-dependencies
    statement: >-
      Declare component dependencies
      alongside their interfaces.
    scope: [architecture]
    ruled:
      date: "2026-01-07"
      source: synthetic-ruling-07
    enforcement: >-
      Compare dependency declarations
      with component usage.
    status: active

  - id: bound-delegated-work
    statement: >-
      Bound each delegated task
      to the agreed responsibility.
    scope: [delegation]
    ruled:
      date: "2026-01-08"
      source: synthetic-ruling-08
    enforcement: >-
      Review task scope
      before accepting completion.
    status: active
"""


def _request(
    file_text: str = REGISTRY_TEXT, scopes: tuple[str, ...] = ("delegation",)
) -> ModelInvariantsSelectRequest:
    return ModelInvariantsSelectRequest(
        file_text=file_text,
        scopes=scopes,
        source_path=SOURCE_PATH,
        commit_sha=SHA,
        as_of=AS_OF,
    )


def test_invariants_select_active_delegation_in_file_order() -> None:
    registry = yaml.safe_load(REGISTRY_TEXT)
    expected_ids = [
        record["id"]
        for record in registry["invariants"]
        if record["status"] == "active" and "delegation" in record["scope"]
    ]
    assert len(expected_ids) == 4

    result = HandlerContextInvariantsSelect().handle(_request())

    assert result.status is EnumContextSourceStatus.RESOLVED
    assert [item.invariant_id for item in result.selected] == expected_ids


def test_invariants_select_preserves_fields_text_and_line_spans() -> None:
    registry = yaml.safe_load(REGISTRY_TEXT)
    records = {record["id"]: record for record in registry["invariants"]}
    lines = REGISTRY_TEXT.splitlines(keepends=True)
    result = HandlerContextInvariantsSelect().handle(_request())
    assert len(result.selected) == 4

    for item in result.selected:
        record = records[item.invariant_id]
        entry = item.entry
        assert item.statement == record["statement"]
        assert item.enforcement == record["enforcement"]
        assert entry.text in REGISTRY_TEXT
        assert 1 <= item.line_start <= item.line_end <= len(lines)
        assert "".join(lines[item.line_start - 1 : item.line_end]) == entry.text
        assert entry.paraphrase is True
        assert entry.verbatim_verified is False
        assert entry.factor is EnumContextFactor.INVARIANTS
        assert entry.supersession_status is EnumContextSupersessionStatus.ACTIVE
        assert (
            entry.source_ref == f"reference/invariants.yaml@{SHA}#{item.invariant_id}"
        )
        assert entry.span is not None
        assert entry.span.unit is EnumContextSpanUnit.LINE
        assert entry.span.start == item.line_start
        assert entry.span.end == item.line_end

        # Find the raw scalar independently of the handler's reported span.
        item_start = lines.index(f"  - id: {item.invariant_id}\n")
        statement_start = lines.index("    statement: >-\n", item_start)
        statement_end = statement_start + 1
        while lines[statement_end].startswith("      "):
            statement_end += 1
        raw_statement = "".join(lines[statement_start:statement_end])
        assert statement_end - statement_start >= 3
        assert raw_statement in entry.text
        assert "    enforcement: >-\n" in entry.text


def test_invariants_select_excludes_all_superseded_entries() -> None:
    registry = yaml.safe_load(REGISTRY_TEXT)
    superseded = [
        record for record in registry["invariants"] if record["status"] == "superseded"
    ]
    assert len(superseded) == 2
    assert any("delegation" not in record["scope"] for record in superseded)

    result = HandlerContextInvariantsSelect().handle(_request())

    assert [item.invariant_id for item in result.excluded] == [
        record["id"] for record in superseded
    ]
    for exclusion, record in zip(result.excluded, superseded, strict=True):
        assert exclusion.reason is EnumInvariantExclusionReason.SUPERSEDED
        assert exclusion.superseded_by == record["superseded_by"]
    selected_ids = {item.invariant_id for item in result.selected}
    assert selected_ids.isdisjoint(record["id"] for record in superseded)


def test_invariants_select_has_no_budget_or_selection_hash() -> None:
    statement = "x" * 2000
    oversize_registry = (
        "schema_version: 1\nscopes: [architecture, delegation, process]\ninvariants:\n"
    ) + "\n".join(
        f"  - id: synthetic-{index:03d}\n"
        f"    statement: >-\n      {statement}\n"
        "    scope: [delegation]\n"
        '    ruled: {date: "2026-01-01", source: synthetic-ruling}\n'
        "    enforcement: >-\n      Check completion.\n"
        "    status: active\n"
        for index in range(300)
    )
    parsed = yaml.safe_load(oversize_registry)
    assert len(parsed["invariants"]) == 300
    assert all(len(record["statement"]) == 2000 for record in parsed["invariants"])

    result = HandlerContextInvariantsSelect().handle(_request(oversize_registry))

    assert result.status is EnumContextSourceStatus.RESOLVED
    assert len(result.selected) == 300
    assert [item.invariant_id for item in result.selected] == [
        record["id"] for record in parsed["invariants"]
    ]
    assert all(item.statement == statement for item in result.selected)
    assert not any(
        "hash" in name or "budget" in name
        for name in ModelInvariantsSelectResult.model_fields
    )


def test_invariants_select_no_active_scope_is_unresolved() -> None:
    result = HandlerContextInvariantsSelect().handle(_request(scopes=("process",)))

    assert result.status is EnumContextSourceStatus.UNRESOLVED
    assert result.selected == ()
    assert result.error_code is None


def test_invariants_select_unknown_scope_is_unavailable() -> None:
    result = HandlerContextInvariantsSelect().handle(_request(scopes=("unknown",)))

    assert result.status is EnumContextSourceStatus.UNAVAILABLE
    assert result.error_code is EnumInvariantsSelectError.UNKNOWN_SCOPE
    assert result.selected == ()


def test_invariants_select_unknown_entry_key_is_invalid_registry() -> None:
    invalid_registry = REGISTRY_TEXT.replace(
        "  - id: delegate-explicitly\n",
        "  - id: delegate-explicitly\n    owner: x\n",
        1,
    )
    result = HandlerContextInvariantsSelect().handle(_request(invalid_registry))

    assert result.status is EnumContextSourceStatus.UNAVAILABLE
    assert result.error_code is EnumInvariantsSelectError.INVALID_REGISTRY
    assert result.selected == ()


def test_invariants_select_duplicate_id_is_invalid_registry() -> None:
    invalid_registry = REGISTRY_TEXT.replace(
        "  - id: bound-delegated-work\n", "  - id: delegate-explicitly\n", 1
    )
    result = HandlerContextInvariantsSelect().handle(_request(invalid_registry))

    assert result.status is EnumContextSourceStatus.UNAVAILABLE
    assert result.error_code is EnumInvariantsSelectError.INVALID_REGISTRY
    assert result.selected == ()


def test_invariants_select_superseded_without_successor_is_invalid_registry() -> None:
    invalid_registry = REGISTRY_TEXT.replace(
        "    superseded_by: delegate-explicitly\n", "", 1
    )
    result = HandlerContextInvariantsSelect().handle(_request(invalid_registry))

    assert result.status is EnumContextSourceStatus.UNAVAILABLE
    assert result.error_code is EnumInvariantsSelectError.INVALID_REGISTRY
    assert result.selected == ()


def test_invariants_select_is_deterministic() -> None:
    request = _request()
    handler = HandlerContextInvariantsSelect()

    first = handler.handle(request)
    second = handler.handle(request)

    assert first == second
    assert first.model_dump_json() == second.model_dump_json()


def test_invariants_select_contract_and_entry_point_resolve() -> None:
    contract = yaml.safe_load((NODE / "contract.yaml").read_text(encoding="utf-8"))
    assert contract["event_bus"]["subscribe_topics"] == [
        "onex.cmd.omnimarket.context-invariants-select-requested.v1"
    ]
    assert contract["event_bus"]["publish_topics"] == [
        "onex.evt.omnimarket.context-invariants-select-completed.v1"
    ]
    assert contract["descriptor"]["purity"] == "pure"
    routes = contract["handler_routing"]["handlers"]
    assert len(routes) == 1
    route = routes[0]
    declaration = route["handler"]
    handler_module = importlib.import_module(declaration["module"])
    assert (
        getattr(handler_module, declaration["name"]) is HandlerContextInvariantsSelect
    )
    for key, expected in (
        ("input_model", ModelInvariantsSelectRequest),
        ("output_model", ModelInvariantsSelectResult),
    ):
        module_name, class_name = route[key].rsplit(".", 1)
        assert getattr(importlib.import_module(module_name), class_name) is expected

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["entry-points"]["onex.nodes"][NODE_NAME] == PACKAGE


def test_invariants_select_handler_imports_are_pure() -> None:
    source = (NODE / "handlers/handler_context_invariants_select.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    imported_roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported_roots.add(node.module.split(".")[0])
    forbidden = {
        "subprocess",
        "socket",
        "requests",
        "httpx",
        "os",
        "pathlib",
        "time",
        "aiokafka",
        "psycopg",
    }
    assert imported_roots.isdisjoint(forbidden), imported_roots & forbidden


def test_invariants_select_no_private_names() -> None:
    # The private names are assembled from parts so this file never matches them.
    private_names = re.compile(
        "|".join(
            ".".join(parts)
            for parts in (
                ("knowledge", "base", "internal"),
                ("omninode", "infra"),
                ("omnibase", "internal"),
            )
        ),
        re.IGNORECASE,
    )
    files = [
        path
        for directory in (NODE, Path(__file__).resolve().parent)
        for path in sorted(directory.rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts
    ]
    assert files
    named = [
        str(path.relative_to(ROOT))
        for path in files
        if private_names.search(path.read_text(encoding="utf-8", errors="replace"))
    ]
    assert not named, named
