# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain for the doc-section select compute (OMN-21126): verbatim sections, plan lifecycle, ambiguity, data-only policy."""

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
from pydantic import ValidationError

from omnimarket.nodes.node_context_doc_section_select_compute.handlers.handler_context_doc_section_select import (
    HandlerContextDocSectionSelect,
)
from omnimarket.nodes.node_context_doc_section_select_compute.models.model_context_doc_section_select import (
    EnumDocSectionExclusionReason,
    EnumDocSectionSelectError,
    ModelDocSectionDocument,
    ModelDocSectionRule,
    ModelDocSectionSelectRequest,
    ModelDocSectionSelectResult,
    ModelDocSectionTier,
    ModelPlanLifecycleRow,
)

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
NODE_NAME = "node_context_doc_section_select_compute"
PACKAGE = f"omnimarket.nodes.{NODE_NAME}"
NODE = ROOT / "src/omnimarket/nodes" / NODE_NAME
SHA = "0123456789abcdef0123456789abcdef01234567"
AS_OF = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)

SHAPES_A = "architecture/shapes-a.md"
SHAPES_B = "architecture/shapes-b.md"
PATTERNS = "reference/patterns.md"
PLAN_LIVE = "plans/live-plan.md"
PLAN_STALLED = "plans/stalled-plan.md"
PLAN_UNLISTED = "plans/unlisted-plan.md"

COMPUTE_SHAPE_A = (
    "## Compute shape\n"
    "\n"
    "A compute node is  pure; two spaces are kept,\n"
    "```\n"
    "# not a heading, inside a fence\n"
    "```\n"
    "\n"
    "### Compute inputs\n"
    "\n"
    "Typed models only.\n"
)
EFFECT_SHAPE_A = "## Effect shape\n\nAn effect node owns the I/O.\n"
COMPUTE_SHAPE_B = "## Compute shape\n\nA compute node may call a clock.\n"

DOCS: dict[str, str] = {
    SHAPES_A: (
        "# Shapes A\n\nPreamble kept out of every section.\n\n"
        + COMPUTE_SHAPE_A
        + "\n"
        + EFFECT_SHAPE_A
        + "\n## Reducer shape\n\nA reducer folds events.\n"
    ),
    SHAPES_B: "# Shapes B\n\n" + COMPUTE_SHAPE_B,
    PATTERNS: (
        "# Patterns\n\n## Handler wiring\n\nSee `src/omnimarket/nodes/` and "
        "`docs/missing-guide.md`, also `scripts/ci/check_nodes.py` and "
        "https://example.invalid/a/b.\n\n## Unrelated\n\nNot requested.\n"
    ),
    PLAN_LIVE: "# Live\n\n## Tasks\n\nBuild the thing.\n\n## Risks\n\nNone.\n",
    PLAN_STALLED: "# Stalled\n\n## Tasks\n\nOld work.\n",
    PLAN_UNLISTED: "# Unlisted\n\n## Tasks\n\nNobody tracks this.\n",
}

TREE = (
    "src/omnimarket/nodes/node_a/contract.yaml",
    "scripts/ci/check_nodes.py",
    "docs/index.md",
)

TIERS = (
    ModelDocSectionTier(tier_id="shapes", factor=EnumContextFactor.CANONICAL_SHAPES),
    ModelDocSectionTier(
        tier_id="patterns", factor=EnumContextFactor.ARCHITECTURE_PATTERNS
    ),
    ModelDocSectionTier(tier_id="plans", factor=EnumContextFactor.PLAN_EXCERPTS),
)

RULES = (
    ModelDocSectionRule(
        tier_id="shapes",
        document_path=SHAPES_A,
        heading="Effect shape",
        always_inject=True,
    ),
    ModelDocSectionRule(
        tier_id="shapes",
        document_path=SHAPES_A,
        heading="Compute shape",
        task_kinds=("compute",),
    ),
    ModelDocSectionRule(
        tier_id="shapes",
        document_path=SHAPES_A,
        heading="Reducer shape",
        task_kinds=("reducer",),
    ),
    ModelDocSectionRule(
        tier_id="patterns",
        document_path=PATTERNS,
        heading="Handler wiring",
        task_kinds=("compute", "effect"),
    ),
    ModelDocSectionRule(
        tier_id="plans",
        document_path=PLAN_LIVE,
        heading="Tasks",
        always_inject=True,
    ),
    ModelDocSectionRule(
        tier_id="plans",
        document_path=PLAN_STALLED,
        heading="Tasks",
        always_inject=True,
    ),
    ModelDocSectionRule(
        tier_id="plans",
        document_path=PLAN_UNLISTED,
        heading="Tasks",
        always_inject=True,
    ),
)

LIFECYCLE = (
    ModelPlanLifecycleRow(path=PLAN_LIVE, status="active", resolved_status="active"),
    ModelPlanLifecycleRow(
        path=PLAN_STALLED, status="stalled", resolved_status="stalled"
    ),
)


def _documents(
    corpus: dict[str, str] | None = None,
) -> tuple[ModelDocSectionDocument, ...]:
    return tuple(
        ModelDocSectionDocument(path=path, file_text=text)
        for path, text in (corpus if corpus is not None else DOCS).items()
    )


def _request(
    corpus: dict[str, str] | None = None,
    *,
    task_kind: str | None = "compute",
    rules: tuple[ModelDocSectionRule, ...] = RULES,
    tiers: tuple[ModelDocSectionTier, ...] = TIERS,
    lifecycle: tuple[ModelPlanLifecycleRow, ...] = LIFECYCLE,
    tree_paths: tuple[str, ...] = TREE,
) -> ModelDocSectionSelectRequest:
    return ModelDocSectionSelectRequest(
        commit_sha=SHA,
        as_of=AS_OF,
        task_kind=task_kind,
        tiers=tiers,
        rules=rules,
        documents=_documents(corpus),
        plan_lifecycle=lifecycle,
        tree_paths=tree_paths,
    )


def _select(
    corpus: dict[str, str] | None = None,
    *,
    task_kind: str | None = "compute",
    rules: tuple[ModelDocSectionRule, ...] = RULES,
    tiers: tuple[ModelDocSectionTier, ...] = TIERS,
    lifecycle: tuple[ModelPlanLifecycleRow, ...] = LIFECYCLE,
    tree_paths: tuple[str, ...] = TREE,
) -> ModelDocSectionSelectResult:
    request = _request(
        corpus,
        task_kind=task_kind,
        rules=rules,
        tiers=tiers,
        lifecycle=lifecycle,
        tree_paths=tree_paths,
    )
    return HandlerContextDocSectionSelect().handle(request)


def _excluded(
    result: ModelDocSectionSelectResult, reason: EnumDocSectionExclusionReason
) -> list[str]:
    return [
        f"{item.path}#{item.heading}"
        for item in result.excluded
        if item.reason is reason
    ]


def test_doc_section_select_every_excerpt_is_a_contiguous_substring_at_its_span() -> (
    None
):
    result = _select()

    assert result.status is EnumContextSourceStatus.RESOLVED
    assert result.selected
    for item in result.selected:
        entry = item.entry
        file_text = DOCS[item.path]
        assert entry.text in file_text
        assert entry.span is not None
        assert entry.span.unit is EnumContextSpanUnit.LINE
        lines = file_text.splitlines(keepends=True)
        assert "".join(lines[entry.span.start : entry.span.end]) == entry.text
        assert entry.source_ref == f"{item.path}@{SHA}"
        assert entry.verbatim_verified is True
        assert entry.paraphrase is False
        assert entry.status is EnumContextSourceStatus.RESOLVED
        assert entry.supersession_status is EnumContextSupersessionStatus.ACTIVE
        assert entry.as_of == AS_OF


def test_doc_section_select_section_runs_to_the_next_heading_of_the_same_or_higher_level() -> (
    None
):
    result = _select(task_kind="compute")

    compute = next(item for item in result.selected if item.heading == "Compute shape")
    # The sub-heading and the fenced "# not a heading" line stay inside the section.
    assert compute.entry.text == COMPUTE_SHAPE_A.rstrip("\n") + "\n"
    assert "### Compute inputs" in compute.entry.text
    assert "# not a heading, inside a fence" in compute.entry.text
    assert "Effect shape" not in compute.entry.text
    assert "Preamble kept out of every section." not in compute.entry.text


def test_doc_section_select_always_inject_is_unconditional_and_triggers_come_from_rules() -> (
    None
):
    unconditional = {
        (item.path, item.heading) for item in _select(task_kind=None).selected
    }
    assert (SHAPES_A, "Effect shape") in unconditional
    assert (SHAPES_A, "Compute shape") not in unconditional
    assert (PATTERNS, "Handler wiring") not in unconditional

    reducer = {
        (item.path, item.heading) for item in _select(task_kind="reducer").selected
    }
    assert (SHAPES_A, "Effect shape") in reducer
    assert (SHAPES_A, "Reducer shape") in reducer
    assert (SHAPES_A, "Compute shape") not in reducer

    # Different rule data, same documents: the policy lives in the request, not the node.
    only_unrelated = (
        ModelDocSectionRule(
            tier_id="patterns",
            document_path=PATTERNS,
            heading="Unrelated",
            task_kinds=("anything",),
        ),
    )
    swapped = _select(task_kind="anything", rules=only_unrelated)
    assert [(item.path, item.heading) for item in swapped.selected] == [
        (PATTERNS, "Unrelated")
    ]


def test_doc_section_select_unknown_rule_key_and_triggerless_rule_fail_validation() -> (
    None
):
    with pytest.raises(ValidationError):
        ModelDocSectionRule.model_validate(
            {
                "tier_id": "shapes",
                "document_path": SHAPES_A,
                "heading": "Effect shape",
                "always_inject": True,
                "always_inejct": True,
            }
        )
    with pytest.raises(ValidationError):
        ModelDocSectionRule(
            tier_id="shapes", document_path=SHAPES_A, heading="Effect shape"
        )
    with pytest.raises(ValidationError):
        ModelDocSectionTier.model_validate(
            {"tier_id": "shapes", "factor": "canonical_shapes", "budget": 100}
        )
    with pytest.raises(ValidationError):
        ModelDocSectionTier(tier_id="state", factor=EnumContextFactor.OPEN_WORK)
    with pytest.raises(ValidationError):
        _request(
            rules=(
                ModelDocSectionRule(
                    tier_id="nowhere",
                    document_path=SHAPES_A,
                    heading="Effect shape",
                    always_inject=True,
                ),
            )
        )


def test_doc_section_select_plan_not_active_or_not_listed_is_excluded() -> None:
    result = _select(task_kind=None)

    plans = {item.path: item for item in result.selected if item.tier_id == "plans"}
    assert set(plans) == {PLAN_LIVE}
    assert plans[PLAN_LIVE].resolved_status == "active"
    assert plans[PLAN_LIVE].entry.factor is EnumContextFactor.PLAN_EXCERPTS
    assert _excluded(result, EnumDocSectionExclusionReason.PLAN_NOT_ACTIVE) == [
        f"{PLAN_STALLED}#Tasks"
    ]
    assert _excluded(result, EnumDocSectionExclusionReason.PLAN_NOT_LISTED) == [
        f"{PLAN_UNLISTED}#Tasks"
    ]


def test_doc_section_select_shape_defined_differently_in_two_documents_is_ambiguous() -> (
    None
):
    rules = (
        ModelDocSectionRule(
            tier_id="shapes",
            document_path=SHAPES_A,
            heading="Compute shape",
            task_kinds=("compute",),
        ),
        ModelDocSectionRule(
            tier_id="shapes",
            document_path=SHAPES_B,
            heading="Compute shape",
            task_kinds=("compute",),
        ),
    )

    result = _select(rules=rules, tiers=TIERS[:1])

    assert result.status is EnumContextSourceStatus.AMBIGUOUS
    assert result.selected == ()
    assert sorted(
        _excluded(result, EnumDocSectionExclusionReason.AMBIGUOUS_DEFINITION)
    ) == [
        f"{SHAPES_A}#Compute shape",
        f"{SHAPES_B}#Compute shape",
    ]


def test_doc_section_select_ambiguity_keeps_the_unambiguous_sections() -> None:
    rules = (
        *RULES[:2],
        ModelDocSectionRule(
            tier_id="shapes",
            document_path=SHAPES_B,
            heading="Compute shape",
            task_kinds=("compute",),
        ),
    )

    result = _select(rules=rules, tiers=TIERS[:1])

    assert result.status is EnumContextSourceStatus.RESOLVED
    assert [(item.path, item.heading) for item in result.selected] == [
        (SHAPES_A, "Effect shape")
    ]
    assert (
        len(_excluded(result, EnumDocSectionExclusionReason.AMBIGUOUS_DEFINITION)) == 2
    )


def test_doc_section_select_identical_definitions_in_two_documents_are_both_admitted() -> (
    None
):
    corpus = {**DOCS, SHAPES_B: "# Shapes B\n\n" + COMPUTE_SHAPE_A}
    rules = (
        ModelDocSectionRule(
            tier_id="shapes",
            document_path=SHAPES_A,
            heading="Compute shape",
            task_kinds=("compute",),
        ),
        ModelDocSectionRule(
            tier_id="shapes",
            document_path=SHAPES_B,
            heading="Compute shape",
            task_kinds=("compute",),
        ),
    )

    result = _select(corpus, rules=rules, tiers=TIERS[:1])

    assert [item.path for item in result.selected] == [SHAPES_A, SHAPES_B]
    assert result.excluded == ()


def test_doc_section_select_missing_and_duplicate_headings_are_listed_not_guessed() -> (
    None
):
    corpus = {
        **DOCS,
        SHAPES_B: "# B\n\n## Twice\n\nfirst\n\n## Twice\n\nsecond\n",
    }
    rules = (
        ModelDocSectionRule(
            tier_id="shapes",
            document_path=SHAPES_A,
            heading="Not there",
            always_inject=True,
        ),
        ModelDocSectionRule(
            tier_id="shapes",
            document_path=SHAPES_B,
            heading="Twice",
            always_inject=True,
        ),
        ModelDocSectionRule(
            tier_id="shapes",
            document_path="architecture/absent.md",
            heading="Anything",
            always_inject=True,
        ),
    )

    result = _select(corpus, rules=rules, tiers=TIERS[:1])

    assert result.status is EnumContextSourceStatus.UNRESOLVED
    assert result.selected == ()
    assert sorted(_excluded(result, EnumDocSectionExclusionReason.SECTION_MISSING)) == [
        "architecture/absent.md#Anything",
        f"{SHAPES_A}#Not there",
    ]
    assert _excluded(result, EnumDocSectionExclusionReason.DUPLICATE_HEADING) == [
        f"{SHAPES_B}#Twice"
    ]


def test_doc_section_select_reports_ungrounded_cited_paths_against_the_tree_index() -> (
    None
):
    result = _select(task_kind="effect")

    wiring = next(item for item in result.selected if item.heading == "Handler wiring")
    grounding = {cited.path: cited.grounded for cited in wiring.cited_paths}
    assert grounding == {
        "src/omnimarket/nodes/": True,
        "docs/missing-guide.md": False,
        "scripts/ci/check_nodes.py": True,
    }

    without_tree = _select(task_kind="effect", tree_paths=())
    bare = next(
        item for item in without_tree.selected if item.heading == "Handler wiring"
    )
    assert all(cited.grounded is False for cited in bare.cited_paths)
    assert len(bare.cited_paths) == 3


def test_doc_section_select_order_is_tier_then_path_and_ignores_input_order() -> None:
    forward = _select(task_kind=None)
    backward = HandlerContextDocSectionSelect().handle(
        _request(
            dict(reversed(list(DOCS.items()))),
            task_kind=None,
            rules=tuple(reversed(RULES)),
        )
    )

    assert forward == backward
    assert forward.model_dump_json() == backward.model_dump_json()
    tier_order = [item.tier_id for item in forward.selected]
    assert tier_order == sorted(tier_order, key=["shapes", "patterns", "plans"].index)


def test_doc_section_select_duplicate_document_path_is_unavailable() -> None:
    documents = (*_documents(), ModelDocSectionDocument(path=SHAPES_A, file_text="x"))
    request = _request().model_copy(update={"documents": documents})

    result = HandlerContextDocSectionSelect().handle(request)

    assert result.status is EnumContextSourceStatus.UNAVAILABLE
    assert result.error_code is EnumDocSectionSelectError.DUPLICATE_PATH
    assert result.selected == ()


def test_doc_section_select_has_no_budget_or_selection_hash() -> None:
    body = "x" * 4000 + "\n"
    corpus = {
        f"reference/doc-{index:03d}.md": f"# Doc {index}\n\n## Section\n\n{body}"
        for index in range(1, 301)
    }
    rules = tuple(
        ModelDocSectionRule(
            tier_id="patterns",
            document_path=path,
            heading="Section",
            always_inject=True,
        )
        for path in corpus
    )

    result = _select(corpus, rules=rules, tiers=TIERS[1:2], task_kind=None)

    assert result.status is EnumContextSourceStatus.RESOLVED
    assert len(result.selected) == 300
    assert all(body in item.entry.text for item in result.selected)
    assert not any(
        "hash" in name or "budget" in name
        for name in ModelDocSectionSelectResult.model_fields
    )


def test_doc_section_select_request_refuses_a_short_sha() -> None:
    with pytest.raises(ValidationError):
        ModelDocSectionSelectRequest(
            commit_sha="abc123",
            as_of=AS_OF,
            tiers=TIERS,
            rules=RULES,
            documents=_documents(),
        )


def test_doc_section_select_contract_and_entry_point_resolve() -> None:
    contract = yaml.safe_load((NODE / "contract.yaml").read_text(encoding="utf-8"))
    assert contract["event_bus"]["subscribe_topics"] == [
        "onex.cmd.omnimarket.context-doc-section-select-requested.v1"
    ]
    assert contract["event_bus"]["publish_topics"] == [
        "onex.evt.omnimarket.context-doc-section-select-completed.v1"
    ]
    assert contract["descriptor"]["purity"] == "pure"
    routes = contract["handler_routing"]["handlers"]
    assert len(routes) == 1
    route = routes[0]
    declaration = route["handler"]
    handler_module = importlib.import_module(declaration["module"])
    assert (
        getattr(handler_module, declaration["name"]) is HandlerContextDocSectionSelect
    )
    for key, expected in (
        ("input_model", ModelDocSectionSelectRequest),
        ("output_model", ModelDocSectionSelectResult),
    ):
        module_name, class_name = route[key].rsplit(".", 1)
        assert getattr(importlib.import_module(module_name), class_name) is expected

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["entry-points"]["onex.nodes"][NODE_NAME] == PACKAGE


def test_doc_section_select_handler_imports_are_pure() -> None:
    source = (NODE / "handlers/handler_context_doc_section_select.py").read_text(
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
    # Another node's private modules are never imported: shared types are promoted instead.
    other_nodes = [
        line
        for line in source.splitlines()
        if re.match(r"\s*(from|import) omnimarket\.nodes\.node_", line)
        and NODE_NAME not in line
    ]
    assert not other_nodes, other_nodes


def test_doc_section_select_no_private_names() -> None:
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
