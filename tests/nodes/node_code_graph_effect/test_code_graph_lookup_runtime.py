# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lookup as the runtime binds it: overlay, git clones, the envelope-shaped handler (OMN-23648)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.models.model_code_graph_lookup import EnumCodeGraphQuestion
from omnimarket.nodes.node_code_graph_effect import runtime_code_graph
from omnimarket.nodes.node_code_graph_effect.handlers.handler_code_graph_requested import (
    HandlerCodeGraphLookupRequested,
)
from tests.nodes.node_code_graph_effect.code_graph_support import (
    APP,
    CORE,
    EXAMPLE,
    make_clone,
)
from tests.nodes.node_code_graph_effect.lookup_support import ask

pytestmark = pytest.mark.unit


def _install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, lookup: bool = True
) -> None:
    clones = tmp_path / "clones"
    make_clone(clones, "example_core", CORE)
    make_clone(clones, "example_app", APP)
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    document["graph_backend"] = "git"
    if not lookup:
        del document["lookup"]
    target = tmp_path / "overlays" / "node_code_graph_effect" / "overlay.yaml"
    target.parent.mkdir(parents=True)
    target.write_text(yaml.safe_dump(document), encoding="utf-8")
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path / "overlays"))
    monkeypatch.setenv("EXAMPLE_REPOS_ROOT", str(clones))
    runtime_code_graph._VERSIONS.clear()


def test_the_example_overlay_declares_the_retention() -> None:
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    assert document["lookup"] == {"retention_versions": 3}


def test_the_bound_handler_answers_a_definition_from_a_git_clone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(tmp_path, monkeypatch)
    result = HandlerCodeGraphLookupRequested().handle(
        ask(EnumCodeGraphQuestion.DEFINITION, repo="example_core", symbol="Thing")
    )
    assert result.status == "found"
    assert [(m.path, m.line) for m in result.matches] == [
        ("src/example_core/models.py", 1)
    ]
    assert result.answered_from is not None
    assert len(result.answered_from) == 40


def test_an_unstored_commit_is_not_found_and_names_the_nearest_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(tmp_path, monkeypatch)
    head = HandlerCodeGraphLookupRequested().handle(
        ask(EnumCodeGraphQuestion.DEFINITION, repo="example_core", symbol="Thing")
    )
    missing = HandlerCodeGraphLookupRequested().handle(
        ask(
            EnumCodeGraphQuestion.DEFINITION,
            repo="example_core",
            symbol="Thing",
            as_of="f" * 40,
        )
    )
    assert missing.status == "not-found"
    assert missing.answered_from is None
    assert missing.nearest is not None
    assert missing.nearest.commit == head.answered_from
    assert missing.nearest.ordering == "unresolved"


def test_an_overlay_with_no_lookup_block_is_a_typed_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(tmp_path, monkeypatch, lookup=False)
    result = HandlerCodeGraphLookupRequested().handle(
        ask(EnumCodeGraphQuestion.DEFINITION, repo="example_core", symbol="Thing")
    )
    assert (result.status, result.reason) == (
        "failed",
        "the overlay has no lookup block",
    )


def test_a_repository_the_overlay_does_not_declare_is_a_typed_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(tmp_path, monkeypatch)
    result = HandlerCodeGraphLookupRequested().handle(
        ask(EnumCodeGraphQuestion.DEFINITION, repo="elsewhere", symbol="Thing")
    )
    assert result.status == "failed"
    assert result.reason == "the overlay does not declare elsewhere"
