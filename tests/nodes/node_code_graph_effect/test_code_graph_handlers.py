# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The handlers question: per contract, per operation, the handler and its models (OMN-26337, AC1).

A fixture repository holds one contract with two operations, one of them naming a handler class the
repository does not define, and a contract whose only operation has no route. The answer holds one
row per ``handler_routing`` operation, counted independently from the fixture's YAML.
"""

from __future__ import annotations

import pytest
import yaml

from omnimarket.models.model_code_graph_handlers import (
    EnumHandlerReference,
    EnumHandlerResolution,
    EnumHandlerRoute,
    EnumUnresolvedReason,
)
from omnimarket.models.model_code_graph_lookup import (
    EnumCodeGraphQuestion,
    ModelCodeGraphLookupCommand,
    ModelCodeGraphLookupResult,
)
from omnimarket.nodes.node_code_graph_effect.adapters.version_stores import (
    InMemoryCommitClock,
    InMemoryVersionStore,
)
from omnimarket.nodes.node_code_graph_effect.handlers.code_graph_handlers import (
    handlers_summary_line,
)
from omnimarket.nodes.node_code_graph_effect.handlers.code_graph_lookup import (
    CodeGraphLookup,
)
from tests.nodes.node_code_graph_effect.lookup_support import (
    BASE,
    CORRELATION,
    REPO,
    version,
)

pytestmark = pytest.mark.unit

ALPHA = "src/example_core/nodes/node_alpha_effect"
QUIET = "src/example_core/nodes/node_quiet_compute"
ALPHA_CONTRACT = f"{ALPHA}/contract.yaml"
QUIET_CONTRACT = f"{QUIET}/contract.yaml"

ALPHA_YAML = """\
name: node_alpha_effect
node_type: effect
terminal_event: onex.evt.example.alpha-done.v1
runtime_dispatch:
  command_topic: onex.cmd.example.alpha.v1
handler_routing:
  routing_strategy: operation_match
  handlers:
    - operation: alpha_run
      handler: {name: HandlerAlpha, module: example_core.nodes.node_alpha_effect.handlers.alpha}
      input_model: example_core.nodes.node_alpha_effect.models.ModelAlphaIn
      output_model: example_core.nodes.node_alpha_effect.models.ModelAlphaOut
      failure_causes: [credentials-unavailable]
      examples:
        - id: first
          input: {name: x}
          expected_output: {ok: true}
    - operation: alpha_missing
      handler: {name: HandlerMissing, module: example_core.nodes.node_alpha_effect.handlers.alpha}
      input_model: example_core.nodes.node_alpha_effect.models.ModelAlphaIn
      output_model: example_core.nodes.node_alpha_effect.models.ModelAlphaOut
"""

QUIET_YAML = """\
name: node_quiet_compute
node_type: compute
handler_routing:
  routing_strategy: operation_match
  handlers:
    - operation: quiet_run
      handler: {name: HandlerQuiet, module: example_core.nodes.node_quiet_compute.handler}
      input_model: pydantic.BaseModel
      output_model: example_core.nodes.node_quiet_compute.models.ModelQuietOut
"""

FILES: dict[str, str] = {
    **BASE,
    f"{ALPHA}/__init__.py": "",
    f"{ALPHA}/handlers/alpha.py": "class HandlerAlpha:\n    pass\n",
    f"{ALPHA}/models.py": (
        "from pydantic import BaseModel\n\n\n"
        "class ModelAlphaIn(BaseModel):\n    name: str\n\n\n"
        "class ModelAlphaOut(BaseModel):\n    ok: bool\n"
    ),
    ALPHA_CONTRACT: ALPHA_YAML,
    f"{QUIET}/handler.py": "class HandlerQuiet:\n    pass\n",
    QUIET_CONTRACT: QUIET_YAML,
}


def _command() -> ModelCodeGraphLookupCommand:
    return ModelCodeGraphLookupCommand(
        correlation_id=CORRELATION, question=EnumCodeGraphQuestion.HANDLERS, repo=REPO
    )


def _answer(files: dict[str, str]) -> ModelCodeGraphLookupResult:
    store = InMemoryVersionStore(retention_versions=3)
    store.put(version("c1", files))
    return CodeGraphLookup(store, InMemoryCommitClock({})).handle(_command())


def _operations_in(files: dict[str, str]) -> list[tuple[str, str]]:
    """The (contract, operation) pairs, counted from the YAML with no help from the graph."""
    pairs: list[tuple[str, str]] = []
    for path, text in sorted(files.items()):
        if not path.endswith("contract.yaml"):
            continue
        document = yaml.safe_load(text)
        pairs.extend(
            (path, entry["operation"])
            for entry in (document.get("handler_routing") or {}).get("handlers", [])
        )
    return pairs


def test_one_contract_two_operations_one_missing_handler_gives_two_rows_one_unresolved() -> (
    None
):
    rows = [
        row for row in _answer(FILES).handler_rows if row.contract == ALPHA_CONTRACT
    ]
    assert [row.operation for row in rows] == ["alpha_missing", "alpha_run"]
    missing, run = rows
    assert run.resolution is EnumHandlerResolution.RESOLVED
    assert run.handler_class == "HandlerAlpha"
    assert run.handler_path == f"{ALPHA}/handlers/alpha.py"
    assert run.input_model_path == f"{ALPHA}/models.py"
    assert run.output_model_path == f"{ALPHA}/models.py"
    assert run.route is EnumHandlerRoute.COMMAND_TOPIC
    assert run.command_topic == "onex.cmd.example.alpha.v1"
    assert run.terminal_event == "onex.evt.example.alpha-done.v1"
    assert run.failure_causes == ("credentials-unavailable",)
    assert [(e.example_id, e.payload, e.expected_output) for e in run.examples] == [
        ("first", {"name": "x"}, {"ok": True})
    ]
    assert run.revision == "c1"
    assert missing.resolution is EnumHandlerResolution.UNRESOLVED
    assert [(u.reference, u.reason) for u in missing.unresolved] == [
        (EnumHandlerReference.HANDLER, EnumUnresolvedReason.CLASS_NOT_DEFINED)
    ]


def test_an_operation_with_no_command_topic_or_subscription_is_no_route_and_a_foreign_model_is_unresolved() -> (
    None
):
    (row,) = [r for r in _answer(FILES).handler_rows if r.contract == QUIET_CONTRACT]
    assert row.route is EnumHandlerRoute.NO_ROUTE
    assert row.resolution is EnumHandlerResolution.UNRESOLVED
    assert [(u.reference, u.reason) for u in row.unresolved] == [
        (
            EnumHandlerReference.INPUT_MODEL,
            EnumUnresolvedReason.MODULE_OUTSIDE_REPOSITORY,
        ),
        (EnumHandlerReference.OUTPUT_MODEL, EnumUnresolvedReason.MODULE_NOT_FOUND),
    ]


def test_row_identities_equal_the_operations_counted_from_the_yaml() -> None:
    result = _answer(FILES)
    assert [(r.contract, r.operation) for r in result.handler_rows] == sorted(
        _operations_in(FILES)
    )
    assert len(result.handler_rows) == 3
    assert result.status == "found"
    assert len(result.matches) == 3


def test_a_dropped_unresolved_row_turns_the_equality_red() -> None:
    """Positive control: a version built without the unresolved row fails the same comparison."""
    full = version("c1", FILES)
    kept = tuple(
        row
        for row in full.handler_rows
        if row.resolution is EnumHandlerResolution.RESOLVED
    )
    store = InMemoryVersionStore(retention_versions=3)
    store.put(full.model_copy(update={"handler_rows": kept}))
    result = CodeGraphLookup(store, InMemoryCommitClock({})).handle(_command())
    assert [(r.contract, r.operation) for r in result.handler_rows] != sorted(
        _operations_in(FILES)
    )


def test_a_subscribed_operation_with_an_observable_output_has_a_subscribe_route() -> (
    None
):
    files = {
        **FILES,
        QUIET_CONTRACT: QUIET_YAML.replace(
            "handler_routing:",
            "event_bus:\n  subscribe_topics: [onex.evt.example.in.v1]\n"
            "  publish_topics: [onex.evt.example.out.v1]\nhandler_routing:",
        ),
    }
    (row,) = [r for r in _answer(files).handler_rows if r.contract == QUIET_CONTRACT]
    assert row.route is EnumHandlerRoute.SUBSCRIBE_TOPIC
    assert row.subscribe_topics == ("onex.evt.example.in.v1",)
    assert row.publish_topics == ("onex.evt.example.out.v1",)


def test_the_rows_do_not_depend_on_the_order_the_files_arrive_in() -> None:
    assert (
        version("c1", FILES).handler_rows
        == version("c1", dict(reversed(FILES.items()))).handler_rows
    )


def test_the_lookup_cli_handlers_line_counts_rows_unresolved_and_no_route() -> None:
    rows = _answer(FILES).handler_rows
    assert handlers_summary_line(REPO, rows) == (
        f"HANDLERS repo={REPO} rows=3 unresolved=2 no_route=1"
    )
    assert handlers_summary_line(REPO, ()) == (
        f"HANDLERS repo={REPO} rows=0 unresolved=0 no_route=0"
    )
