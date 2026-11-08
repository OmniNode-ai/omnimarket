# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The day-one listing of the staged controller copy's private reads (rebuild piece 9)."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_landing_private_reads_effect.handlers.handler_landing_private_reads import (
    HandlerLandingPrivateReads,
)
from omnimarket.nodes.node_landing_private_reads_effect.models.model_landing_private_reads import (
    ModelLandingPrivateReadsRequest,
)
from omnimarket.nodes.node_landing_private_reads_effect.runtime_private_reads import (
    PrivateReadsSourceError,
    read_directory_sources,
    run,
)

from .private_reads_support import overlay, sources, write_copy, write_overlay_root

pytestmark = pytest.mark.unit


def _handle(**kw: object):
    request = ModelLandingPrivateReadsRequest(
        correlation_id="c-1",
        overlay=kw.pop("ov", overlay()),
        sources=kw.pop("src", sources()),
    )
    return HandlerLandingPrivateReads().handle(request)


def test_reads_are_lines_in_a_read_context_only() -> None:
    result = _handle()
    kinds = {(r.line, r.kind) for r in result.reads}
    # line 2 is a comment, 5 a remote slug, 6 a bare label, 7 a pull request ref; 3 and 4 read the root.
    assert kinds == {
        (2, "name_only"),
        (3, "read"),
        (4, "read"),
        (5, "name_only"),
        (6, "name_only"),
        (7, "name_only"),
    }
    assert [(x.file, x.root, x.lines) for x in result.listed] == [
        ("scripts/controller.py", "example-private-tools", [3, 4])
    ]
    assert result.listed[0].destination == "omnimarket node (piece 9)"
    assert result.reads_by_root == {"example-private-tools": 2}


def test_every_read_declared_reads_working() -> None:
    result = _handle()
    assert result.verdict == "WORKING"
    assert result.failure_code is None
    assert result.undeclared == []


def test_an_undeclared_read_reads_not_working_and_is_named() -> None:
    result = _handle(src=sources(planted=True))
    assert result.verdict == "NOT_WORKING"
    assert result.failure_code == "undeclared_private_read"
    assert [(u.file, u.root) for u in result.undeclared] == [
        ("scripts/facts.py", "example-overlay-store")
    ]


def test_no_sources_reads_not_working() -> None:
    result = _handle(src=[])
    assert (result.verdict, result.failure_code) == ("NOT_WORKING", "no_sources")


def test_a_declared_read_that_left_the_copy_is_stale_not_red() -> None:
    result = _handle(src=[sources()[0].model_copy(update={"text": "x = 1"})])
    assert result.verdict == "WORKING"
    assert [(s.file, s.root) for s in result.stale_declared] == [
        ("scripts/controller.py", "example-private-tools")
    ]


def test_directory_backend_reads_only_included_suffixes_outside_excluded_prefixes(
    tmp_path: Path,
) -> None:
    write_copy(tmp_path, planted=True)
    found = read_directory_sources(
        overlay(), {"LANDING_PRIVATE_READS_COPY_ROOT": str(tmp_path)}
    )
    assert [s.path for s in found] == ["scripts/controller.py", "scripts/facts.py"]


def test_directory_backend_refuses_an_unset_root() -> None:
    with pytest.raises(PrivateReadsSourceError):
        read_directory_sources(overlay(), {})


def test_run_loads_the_overlay_from_the_overlay_root(tmp_path: Path) -> None:
    copy = tmp_path / "copy"
    write_copy(copy)
    roots = write_overlay_root(tmp_path / "overlays")
    result = run(
        {
            "ONEX_SKILL_OVERLAY_ROOTS": str(roots),
            "LANDING_PRIVATE_READS_COPY_ROOT": str(copy),
        }
    )
    assert result.verdict == "WORKING"
    assert result.files_scanned == 1


def test_inmemory_backend_takes_the_given_sources(tmp_path: Path) -> None:
    roots = write_overlay_root(tmp_path / "overlays", source_backend="inmemory")
    result = run(
        {"ONEX_SKILL_OVERLAY_ROOTS": str(roots)}, sources=sources(planted=True)
    )
    assert result.failure_code == "undeclared_private_read"
