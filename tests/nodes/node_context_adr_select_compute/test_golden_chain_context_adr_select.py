# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain for the ADR select compute (OMN-21126): selection, provenance, refusals, purity."""

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

from omnimarket.nodes.node_context_adr_select_compute.handlers.handler_context_adr_select import (
    HandlerContextAdrSelect,
)
from omnimarket.nodes.node_context_adr_select_compute.models.model_context_adr_select import (
    EnumAdrExclusionReason,
    EnumAdrSelectError,
    ModelAdrDocument,
    ModelAdrSelectRequest,
    ModelAdrSelectResult,
)

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
NODE_NAME = "node_context_adr_select_compute"
PACKAGE = f"omnimarket.nodes.{NODE_NAME}"
NODE = ROOT / "src/omnimarket/nodes" / NODE_NAME
SHA = "0123456789abcdef0123456789abcdef01234567"
AS_OF = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)


def _adr(
    adr_id: str,
    title: str,
    status: str,
    topics: str,
    superseded_by: str = "[]",
    body: str = "",
    extra: str = "",
) -> str:
    return (
        "---\n"
        "type: adr\n"
        f"status: {status}\n"
        'date: "2026-01-01"\n'
        f'title: "{adr_id}: {title}"\n'
        f"adr_id: {adr_id}\n"
        f"topics: {topics}\n"
        "refs:\n"
        "  - doctrine/synthetic.md\n"
        "supersedes: []\n"
        f"superseded_by: {superseded_by}\n"
        f"{extra}"
        "---\n"
        f"{body or f'# {adr_id}: {title}\n\nSynthetic body for {title}.\n'}"
    )


ACCEPTED_DELEGATION_BODY = (
    "\n# ADR-0003: Delegate explicitly\n"
    "\n## Decision\n"
    "\nAssign a concrete task  with two spaces kept,\n"
    "and a trailing line without a newline."
)

CORPUS: dict[str, str] = {
    "adrs/ADR-0003-delegate-explicitly.md": _adr(
        "ADR-0003",
        "Delegate explicitly",
        "accepted",
        "[delegation, routing]",
        body=ACCEPTED_DELEGATION_BODY,
    ),
    "adrs/ADR-0001-proposed-routing.md": _adr(
        "ADR-0001", "Proposed routing", "proposed", "[delegation]"
    ),
    "adrs/ADR-0002-retired-dispatch.md": _adr(
        "ADR-0002",
        "Retired dispatch",
        "superseded",
        "[delegation]",
        superseded_by="[ADR-0003]",
    ),
    "adrs/ADR-0004-accepted-but-replaced.md": _adr(
        "ADR-0004",
        "Accepted but replaced",
        "accepted",
        "[delegation]",
        superseded_by="[ADR-0005]",
    ),
    "adrs/ADR-0010-parity-ratchet.md": _adr(
        "ADR-0010", "Parity ratchet", "accepted", "[delegation, ci]"
    ),
    "adrs/ADR-0010-tier-cost.md": _adr(
        "ADR-0010", "Tier cost", "accepted", "[delegation, economics]"
    ),
    "adrs/ADR-0011-dashboard-widgets.md": _adr(
        "ADR-0011", "Dashboard widgets", "accepted", "[omnidash]"
    ),
}


def _request(
    corpus: dict[str, str] | None = None,
    topics: tuple[str, ...] = ("delegation",),
) -> ModelAdrSelectRequest:
    documents = corpus if corpus is not None else CORPUS
    return ModelAdrSelectRequest(
        commit_sha=SHA,
        as_of=AS_OF,
        topics=topics,
        documents=tuple(
            ModelAdrDocument(path=path, file_text=text)
            for path, text in documents.items()
        ),
    )


def test_adr_select_admits_accepted_unsuperseded_in_topic_sorted_by_path() -> None:
    result = HandlerContextAdrSelect().handle(_request())

    assert result.status is EnumContextSourceStatus.RESOLVED
    assert [item.path for item in result.selected] == [
        "adrs/ADR-0003-delegate-explicitly.md",
        "adrs/ADR-0010-parity-ratchet.md",
        "adrs/ADR-0010-tier-cost.md",
    ]
    assert [item.adr_id for item in result.selected] == [
        "ADR-0003",
        "ADR-0010",
        "ADR-0010",
    ]
    assert result.error_code is None


def test_adr_select_two_files_sharing_an_id_are_distinct_entries_keyed_by_path() -> (
    None
):
    result = HandlerContextAdrSelect().handle(_request())

    shared = [item for item in result.selected if item.adr_id == "ADR-0010"]
    assert len(shared) == 2
    assert {item.entry.source_ref for item in shared} == {
        f"adrs/ADR-0010-parity-ratchet.md@{SHA}",
        f"adrs/ADR-0010-tier-cost.md@{SHA}",
    }
    assert shared[0].entry.text != shared[1].entry.text


def test_adr_select_body_is_byte_equal_to_the_file_after_its_frontmatter() -> None:
    result = HandlerContextAdrSelect().handle(_request())

    for item in result.selected:
        file_text = CORPUS[item.path]
        _, frontmatter, body = file_text.split("---\n", 2)
        assert item.entry.text == body
        assert file_text.endswith(item.entry.text)
        lines = file_text.splitlines(keepends=True)
        span = item.entry.span
        assert span is not None
        assert span.unit is EnumContextSpanUnit.LINE
        assert "".join(lines[span.start : span.end]) == item.entry.text
        assert span.start == frontmatter.count("\n") + 2
        assert span.end == len(lines)
        assert item.entry.factor is EnumContextFactor.ADRS
        assert item.entry.status is EnumContextSourceStatus.RESOLVED
        assert item.entry.supersession_status is EnumContextSupersessionStatus.ACTIVE
        assert item.entry.verbatim_verified is True
        assert item.entry.paraphrase is False
        assert item.entry.source_ref == f"{item.path}@{SHA}"
        assert item.entry.as_of == AS_OF

    first = result.selected[0]
    assert first.entry.text == ACCEPTED_DELEGATION_BODY
    assert first.title == "ADR-0003: Delegate explicitly"
    assert first.topics == ("delegation", "routing")


def test_adr_select_lists_not_accepted_and_superseded_as_excluded() -> None:
    result = HandlerContextAdrSelect().handle(_request())

    excluded = {item.path: item for item in result.excluded}
    assert set(excluded) == {
        "adrs/ADR-0001-proposed-routing.md",
        "adrs/ADR-0002-retired-dispatch.md",
        "adrs/ADR-0004-accepted-but-replaced.md",
    }
    assert [item.path for item in result.excluded] == sorted(excluded)
    proposed = excluded["adrs/ADR-0001-proposed-routing.md"]
    assert proposed.reason is EnumAdrExclusionReason.NOT_ACCEPTED
    assert proposed.status == "proposed"
    assert proposed.superseded_by == ()
    retired = excluded["adrs/ADR-0002-retired-dispatch.md"]
    assert retired.reason is EnumAdrExclusionReason.SUPERSEDED
    assert retired.superseded_by == ("ADR-0003",)
    replaced = excluded["adrs/ADR-0004-accepted-but-replaced.md"]
    assert replaced.reason is EnumAdrExclusionReason.SUPERSEDED
    assert replaced.status == "accepted"
    assert replaced.superseded_by == ("ADR-0005",)
    assert all(item.source_ref == f"{item.path}@{SHA}" for item in result.excluded)
    # Out-of-topic ADRs are neither selected nor excluded.
    assert "adrs/ADR-0011-dashboard-widgets.md" not in excluded


def test_adr_select_topic_match_is_any_of_the_requested_topics() -> None:
    result = HandlerContextAdrSelect().handle(_request(topics=("omnidash", "ci")))

    assert [item.path for item in result.selected] == [
        "adrs/ADR-0010-parity-ratchet.md",
        "adrs/ADR-0011-dashboard-widgets.md",
    ]


def test_adr_select_order_does_not_depend_on_document_order() -> None:
    reversed_corpus = dict(reversed(list(CORPUS.items())))

    forward = HandlerContextAdrSelect().handle(_request())
    backward = HandlerContextAdrSelect().handle(_request(reversed_corpus))

    assert forward == backward
    assert forward.model_dump_json() == backward.model_dump_json()


def test_adr_select_has_no_budget_or_selection_hash() -> None:
    body = "x" * 4000 + "\n"
    oversize = {
        f"adrs/ADR-{index:04d}-synthetic.md": _adr(
            f"ADR-{index:04d}", "Synthetic", "accepted", "[delegation]", body=body
        )
        for index in range(1, 301)
    }

    result = HandlerContextAdrSelect().handle(_request(oversize))

    assert result.status is EnumContextSourceStatus.RESOLVED
    assert len(result.selected) == 300
    assert all(item.entry.text == body for item in result.selected)
    assert not any(
        "hash" in name or "budget" in name for name in ModelAdrSelectResult.model_fields
    )


def test_adr_select_no_topic_match_is_unresolved() -> None:
    result = HandlerContextAdrSelect().handle(_request(topics=("unlisted",)))

    assert result.status is EnumContextSourceStatus.UNRESOLVED
    assert result.selected == ()
    assert result.error_code is None


def test_adr_select_unknown_frontmatter_key_is_excluded_as_invalid() -> None:
    corpus = dict(CORPUS)
    corpus["adrs/ADR-0020-extra-key.md"] = _adr(
        "ADR-0020",
        "Extra key",
        "accepted",
        "[delegation]",
        extra="owner: someone\n",
    )

    result = HandlerContextAdrSelect().handle(_request(corpus))

    invalid = [
        item
        for item in result.excluded
        if item.reason is EnumAdrExclusionReason.INVALID_FRONTMATTER
    ]
    assert [item.path for item in invalid] == ["adrs/ADR-0020-extra-key.md"]
    assert invalid[0].detail
    assert "adrs/ADR-0020-extra-key.md" not in [item.path for item in result.selected]


@pytest.mark.parametrize(
    "file_text",
    [
        "# No frontmatter at all\n",
        "---\ntype: adr\nstatus: accepted\n",
        "---\n: not yaml [\n---\nbody\n",
        "---\n- a\n- list\n---\nbody\n",
    ],
)
def test_adr_select_malformed_frontmatter_is_excluded_as_invalid(
    file_text: str,
) -> None:
    corpus = dict(CORPUS)
    corpus["adrs/ADR-0021-malformed.md"] = file_text

    result = HandlerContextAdrSelect().handle(_request(corpus))

    assert result.status is EnumContextSourceStatus.RESOLVED
    reasons = {item.path: item.reason for item in result.excluded}
    assert (
        reasons["adrs/ADR-0021-malformed.md"]
        is EnumAdrExclusionReason.INVALID_FRONTMATTER
    )


def test_adr_select_empty_body_is_excluded() -> None:
    _, frontmatter, _ = _adr("ADR-0030", "Empty", "accepted", "[delegation]").split(
        "---\n", 2
    )
    corpus = {"adrs/ADR-0030-empty.md": f"---\n{frontmatter}---\n"}

    result = HandlerContextAdrSelect().handle(_request(corpus))

    assert result.status is EnumContextSourceStatus.UNRESOLVED
    assert [item.reason for item in result.excluded] == [
        EnumAdrExclusionReason.EMPTY_BODY
    ]


def test_adr_select_duplicate_path_is_unavailable() -> None:
    path = "adrs/ADR-0003-delegate-explicitly.md"
    request = ModelAdrSelectRequest(
        commit_sha=SHA,
        as_of=AS_OF,
        topics=("delegation",),
        documents=(
            ModelAdrDocument(path=path, file_text=CORPUS[path]),
            ModelAdrDocument(path=path, file_text=CORPUS[path]),
        ),
    )

    result = HandlerContextAdrSelect().handle(request)

    assert result.status is EnumContextSourceStatus.UNAVAILABLE
    assert result.error_code is EnumAdrSelectError.DUPLICATE_PATH
    assert result.selected == ()


def test_adr_select_request_refuses_a_short_sha_and_empty_topics() -> None:
    with pytest.raises(ValidationError, match="commit_sha"):
        ModelAdrSelectRequest(
            commit_sha="abc123",
            as_of=AS_OF,
            topics=("delegation",),
            documents=(),
        )
    with pytest.raises(ValidationError, match="topics"):
        ModelAdrSelectRequest(
            commit_sha=SHA,
            as_of=AS_OF,
            topics=(),
            documents=(),
        )


def test_adr_select_is_deterministic() -> None:
    request = _request()
    handler = HandlerContextAdrSelect()

    first = handler.handle(request)
    second = handler.handle(request)

    assert first == second
    assert first.model_dump_json() == second.model_dump_json()


def test_adr_select_contract_and_entry_point_resolve() -> None:
    contract = yaml.safe_load((NODE / "contract.yaml").read_text(encoding="utf-8"))
    assert contract["event_bus"]["subscribe_topics"] == [
        "onex.cmd.omnimarket.context-adr-select-requested.v1"
    ]
    assert contract["event_bus"]["publish_topics"] == [
        "onex.evt.omnimarket.context-adr-select-completed.v1"
    ]
    assert contract["descriptor"]["purity"] == "pure"
    routes = contract["handler_routing"]["handlers"]
    assert len(routes) == 1
    route = routes[0]
    declaration = route["handler"]
    handler_module = importlib.import_module(declaration["module"])
    assert getattr(handler_module, declaration["name"]) is HandlerContextAdrSelect
    for key, expected in (
        ("input_model", ModelAdrSelectRequest),
        ("output_model", ModelAdrSelectResult),
    ):
        module_name, class_name = route[key].rsplit(".", 1)
        assert getattr(importlib.import_module(module_name), class_name) is expected

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["entry-points"]["onex.nodes"][NODE_NAME] == PACKAGE


def test_adr_select_handler_imports_are_pure() -> None:
    source = (NODE / "handlers/handler_context_adr_select.py").read_text(
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


def test_adr_select_no_private_names() -> None:
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
