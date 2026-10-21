# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The guidance context source: lane rules and architecture patterns (OMN-21111)."""

from __future__ import annotations

import importlib
import inspect
import tomllib
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from omnibase_core.enums.enum_context_factor import EnumContextFactor
from omnibase_core.enums.enum_context_source_status import EnumContextSourceStatus
from omnibase_core.models.context.model_context_access import ModelContextAccess
from omnibase_core.models.context.model_context_caller_scope import (
    ModelContextCallerScope,
)
from omnibase_core.models.context.model_context_source_request import (
    ModelContextSourceRequest,
)
from omnibase_core.models.context.model_context_source_result import (
    ModelContextSourceResult,
)

from omnimarket.nodes.node_context_pack_builder_compute.parsers.parser_guidance_section import (
    GuidanceSectionParser,
)
from omnimarket.nodes.node_context_source_guidance_effect import (
    HandlerContextSourceGuidance,
    NodeContextSourceGuidanceEffect,
)

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[4]
NODE = "node_context_source_guidance_effect"
NODE_DIR = REPO / "src" / "omnimarket" / "nodes" / NODE
ROOT_ENV = "OMNI_HOME"
LANE_RULES = EnumContextFactor.LANE_RULES
PATTERNS = EnumContextFactor.ARCHITECTURE_PATTERNS

WORKSPACE_GUIDE = """# Guide

## Operating Rules (read first)

### 8. Fail-fast on missing env

Not a lane rule this source names.

### 9. Per-ticket work uses worktrees, not the registry root directly

Per-ticket work uses worktrees under the sanctioned root.

### 10. Local gates are mechanically enforced — no informational bypass

Never use `--no-verify`.

### 17. Never override `core.hooksPath` — in a worktree it is a silent bypass

Never `git stash` in a worktree either.
"""

REPO_GUIDE = """# Repo

## Development Rules

- Keep event topics declared in `contract.yaml`; avoid hardcoded topic strings.

## Common Commands

Not a pattern.
"""


def _root(tmp_path: Path) -> Path:
    (tmp_path / "CLAUDE.md").write_text(WORKSPACE_GUIDE, encoding="utf-8")
    (tmp_path / "omnimarket").mkdir()
    (tmp_path / "omnimarket" / "CLAUDE.md").write_text(REPO_GUIDE, encoding="utf-8")
    return tmp_path


def _read(
    root: Path | None,
    factor: EnumContextFactor,
    *,
    repository: str | None = None,
    revision: str = "2026-10-19T08:00:00Z",
) -> ModelContextSourceResult:
    env = {} if root is None else {ROOT_ENV: str(root)}
    return HandlerContextSourceGuidance(env=env).handle(
        ModelContextSourceRequest(
            factor=factor,
            caller_scope=ModelContextCallerScope(repository=repository),
            access=ModelContextAccess(tenant_id="lab"),
            revision=revision,
        )
    )


def test_lane_rules_are_the_named_sections_whole(tmp_path: Path) -> None:
    result = _read(_root(tmp_path), LANE_RULES)
    assert result.status is EnumContextSourceStatus.RESOLVED
    sections = {
        s.heading_path[-1]: s.content
        for s in GuidanceSectionParser().parse(WORKSPACE_GUIDE, source_file="CLAUDE.md")
    }
    assert [e.source_ref for e in result.entries] == [
        "CLAUDE.md#9. Per-ticket work uses worktrees, not the registry root directly",
        "CLAUDE.md#10. Local gates are mechanically enforced — no informational bypass",
        "CLAUDE.md#17. Never override `core.hooksPath` — in a worktree it is a silent bypass",
    ]
    for entry in result.entries:
        assert entry.source_ref is not None
        assert entry.text == sections[entry.source_ref.split("#", 1)[1]]
        assert entry.verbatim_verified is True
        assert entry.as_of == datetime(2026, 10, 19, 8, 0, 0, tzinfo=UTC)
    assert "Fail-fast" not in "".join(e.text for e in result.entries)


def test_architecture_patterns_read_the_caller_repository(tmp_path: Path) -> None:
    result = _read(_root(tmp_path), PATTERNS, repository="omnimarket")
    assert result.status is EnumContextSourceStatus.RESOLVED
    assert [e.source_ref for e in result.entries] == [
        "omnimarket/CLAUDE.md#Development Rules"
    ]
    assert "Keep event topics declared in `contract.yaml`" in result.entries[0].text


def test_a_repository_the_request_does_not_name_is_skipped(tmp_path: Path) -> None:
    result = _read(_root(tmp_path), PATTERNS)
    assert result.status is EnumContextSourceStatus.UNRESOLVED
    assert result.entry_counts == {"sections": 0, "documents_skipped": 1}


@pytest.mark.parametrize("repository", ["../outside", "omni/../..", "missing-repo"])
def test_a_repository_outside_the_root_or_absent_reads_nothing(
    tmp_path: Path, repository: str
) -> None:
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "CLAUDE.md").write_text(REPO_GUIDE, encoding="utf-8")
    root = tmp_path / "root"
    root.mkdir()
    result = _read(_root(root), PATTERNS, repository=repository)
    assert result.status is EnumContextSourceStatus.UNRESOLVED
    assert result.entries == ()


def test_a_missing_root_is_unavailable() -> None:
    assert _read(None, LANE_RULES).status is EnumContextSourceStatus.UNAVAILABLE


def test_a_revision_that_is_not_a_stamp_takes_the_file_time(tmp_path: Path) -> None:
    result = _read(_root(tmp_path), LANE_RULES, revision="head")
    mtime = datetime.fromtimestamp((tmp_path / "CLAUDE.md").stat().st_mtime, tz=UTC)
    assert {e.as_of for e in result.entries} == {mtime}


def test_a_factor_it_does_not_serve_is_unresolved(tmp_path: Path) -> None:
    result = _read(_root(tmp_path), EnumContextFactor.OPEN_WORK)
    assert result.status is EnumContextSourceStatus.UNRESOLVED


def test_the_contract_declares_its_topics_and_handler_signature() -> None:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text(encoding="utf-8"))
    command = "onex.cmd.omnimarket.context-source-guidance-requested.v1"
    event = "onex.evt.omnimarket.context-source-guidance-completed.v1"
    assert contract["event_bus"]["subscribe_topics"] == [command]
    assert contract["event_bus"]["publish_topics"] == [event]
    assert contract["terminal_event"] == event
    assert contract["context_source_guidance"]["root_env"] == ROOT_ENV
    handler = contract["handler"]
    cls = getattr(importlib.import_module(handler["module"]), handler["class"])
    assert cls is HandlerContextSourceGuidance
    assert issubclass(NodeContextSourceGuidanceEffect, cls)
    hints = inspect.get_annotations(cls.handle, eval_str=True)
    assert hints == {
        "request": ModelContextSourceRequest,
        "return": ModelContextSourceResult,
    }
    assert cls.synchronous_execution is True
    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["project"]["entry-points"]["onex.nodes"][NODE] == (
        f"omnimarket.nodes.{NODE}"
    )


def test_the_handler_hardcodes_no_topic_string() -> None:
    for path in (NODE_DIR / "handlers").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "onex.cmd." not in text, path
        assert "onex.evt." not in text, path
