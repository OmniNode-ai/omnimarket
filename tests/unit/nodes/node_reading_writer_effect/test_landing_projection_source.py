# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lab backend of the landing source: pull request state read from the pr_state projection (OMN-23038).

The projection address comes from the ``node_pr_state_read_effect`` overlay only. Each test serves the
projection from an in-process transport, so no request leaves the process.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import httpx
import pytest
import yaml

from omnimarket.models.model_reading import (
    EnumReadingVerdict,
    ModelReadingWindowCommand,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.landing_adapters import (
    ProjectionLandingSource,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
)
from omnimarket.nodes.node_reading_writer_effect.handlers.handler_reading_writer import (
    HandlerReadingWriter,
)
from omnimarket.nodes.node_reading_writer_effect.overlay import (
    ModelLandingProjectionRoute,
    ModelReadingOverlay,
)
from tests.nodes.node_reading_writer_effect.reading_support import RUN_AT, START, stamp
from tests.unit.nodes.node_reading_writer_effect.landing_support import (
    canary_row,
    landing_overlay_document,
)

pytestmark = pytest.mark.unit

URL = "http://projection.test:3002"
ROUTE_DOCUMENT = {
    "source": "projection",
    "goal_scope_overlay_id": "goal_scope",
    "goal_scope_file_env": "READING_TEST_GOAL_SCOPE",
    "queue_classes": ["green-unarmed", "queue-stuck"],
}


def _overlay() -> ModelReadingOverlay:
    document = landing_overlay_document()
    document["landing_route"] = dict(ROUTE_DOCUMENT)
    return ModelReadingOverlay.model_validate(document)


OVERLAY = _overlay()
ROUTE = OVERLAY.landing_route
REQUIREMENTS = OVERLAY.spec(9).landing
assert isinstance(ROUTE, ModelLandingProjectionRoute)
assert REQUIREMENTS is not None


def _row(
    number: int,
    *,
    state: str = "open",
    watcher_class: str = "green-unarmed",
    draft: bool = False,
    repo: str = "scoped",
    read_minutes_ago: int = 5,
    merged_minutes_ago: int | None = None,
    observed_minutes_ago: int = 1,
) -> dict:
    return {
        "repo": repo,
        "pr_number": number,
        "state": state,
        "head_sha": "a" * 40,
        "base": "dev",
        "head_ref": "omn-1-a",
        "title": "OMN-1 a change",
        "draft": draft,
        "author": "someone",
        "author_is_bot": False,
        "labels": [],
        "armed": False,
        "queued": False,
        "watcher_class": watcher_class,
        "ci_verdict": "GREEN",
        "red_contexts": [],
        "pending_contexts": [],
        "ci_read_at": stamp(RUN_AT - timedelta(minutes=read_minutes_ago)),
        "merged_at": (
            ""
            if merged_minutes_ago is None
            else stamp(RUN_AT - timedelta(minutes=merged_minutes_ago))
        ),
        "observed_at": (RUN_AT - timedelta(minutes=observed_minutes_ago)).isoformat(),
    }


def _environ(tmp_path: Path, *, with_url: bool = True) -> dict[str, str]:
    root = tmp_path / "overlays"
    (root / "node_pr_state_read_effect").mkdir(parents=True)
    if with_url:
        (root / "node_pr_state_read_effect" / "overlay.yaml").write_text(
            yaml.safe_dump({"projection_api_url": URL}), encoding="utf-8"
        )
    scope = tmp_path / "scope.yaml"
    scope.write_text(
        yaml.safe_dump({"ruling": "test", "in_scope_repos": ["scoped"]}),
        encoding="utf-8",
    )
    return {
        "ONEX_SKILL_OVERLAY_ROOTS": str(root),
        ROUTE.goal_scope_file_env: str(scope),
    }


class _Projection:
    """An in-process projection API: records each request and answers a fixed page or status."""

    def __init__(self, rows: list[dict], status: int = 200) -> None:
        self.rows = rows
        self.status = status
        self.requests: list[httpx.Request] = []

    def factory(self, base_url: str) -> httpx.Client:
        def answer(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            if self.status != 200:
                return httpx.Response(self.status, json={"error": "down"})
            return httpx.Response(
                200,
                json={
                    "topic": "onex.snapshot.projection.pr-state.v1",
                    "rows": self.rows,
                    "truncated": False,
                },
            )

        return httpx.Client(base_url=base_url, transport=httpx.MockTransport(answer))


def _source(
    projection: _Projection, environ: dict[str, str]
) -> ProjectionLandingSource:
    return ProjectionLandingSource(ROUTE, environ, client_factory=projection.factory)


def _writer(
    ledger: InMemoryLedger, source: ProjectionLandingSource
) -> HandlerReadingWriter:
    return HandlerReadingWriter(
        overlay=OVERLAY,
        source=ledger,
        appender=ledger,
        clock=lambda: RUN_AT,
        landing=source,
    )


def _window() -> ModelReadingWindowCommand:
    return ModelReadingWindowCommand(
        correlation_id="c-p", window_start=START, pieces=(9,)
    )


def test_the_snapshot_counts_in_scope_state_and_asks_the_address_the_overlay_names(
    tmp_path: Path,
) -> None:
    projection = _Projection(
        [
            _row(1, read_minutes_ago=75),
            _row(2, watcher_class="queue-stuck", read_minutes_ago=10),
            _row(3, draft=True),
            _row(4, watcher_class="red"),
            _row(5, repo="other"),
            _row(6, state="merged", merged_minutes_ago=20),
            _row(7, state="merged", merged_minutes_ago=200),
            _row(8, state="merged", merged_minutes_ago=5, repo="other"),
        ]
    )
    read = _source(projection, _environ(tmp_path)).read(REQUIREMENTS, RUN_AT)
    assert read.error is None
    assert read.snapshot is not None
    waiting = {head.ref: head.waiting_seconds for head in read.snapshot.heads}
    assert waiting == {"scoped#1": 75 * 60, "scoped#2": 10 * 60}
    assert read.snapshot.merges_in_window == 1
    assert read.snapshot.age_seconds == 60
    (request,) = projection.requests
    assert str(request.url).startswith(f"{URL}/projection/")


def test_a_head_waiting_over_an_hour_makes_the_written_reading_not_working(
    tmp_path: Path,
) -> None:
    projection = _Projection([_row(1, read_minutes_ago=75)])
    result = _writer(
        InMemoryLedger([canary_row()]), _source(projection, _environ(tmp_path))
    ).handle(_window())
    (piece,) = result.pieces
    assert piece.verdict is EnumReadingVerdict.NOT_WORKING
    assert piece.reasons == ("head-unmerged-over-limit:scoped-1",)


def test_the_same_head_waiting_inside_the_hour_reads_working(tmp_path: Path) -> None:
    projection = _Projection([_row(1, read_minutes_ago=30)])
    result = _writer(
        InMemoryLedger([canary_row()]), _source(projection, _environ(tmp_path))
    ).handle(_window())
    assert result.pieces[0].verdict is EnumReadingVerdict.WORKING


def test_a_projection_nothing_has_written_to_reads_stale_and_not_working(
    tmp_path: Path,
) -> None:
    projection = _Projection([_row(1, observed_minutes_ago=7 * 24 * 60)])
    result = _writer(
        InMemoryLedger([canary_row()]), _source(projection, _environ(tmp_path))
    ).handle(_window())
    (piece,) = result.pieces
    assert piece.verdict is EnumReadingVerdict.NOT_WORKING
    assert piece.reasons[0].startswith("snapshot-stale:")


def test_without_the_overlay_address_the_read_is_an_error_and_the_piece_reads_unknown(
    tmp_path: Path,
) -> None:
    """The failing control for the address: the overlay value is the only way the source learns it."""
    projection = _Projection([_row(1)])
    environ = _environ(tmp_path, with_url=False)
    read = _source(projection, environ).read(REQUIREMENTS, RUN_AT)
    assert read.snapshot is None
    assert "projection address" in (read.error or "")
    assert projection.requests == []
    result = _writer(
        InMemoryLedger([canary_row()]), _source(projection, environ)
    ).handle(_window())
    assert result.pieces[0].verdict is EnumReadingVerdict.UNKNOWN


def test_an_environment_variable_does_not_stand_in_for_the_overlay_address(
    tmp_path: Path,
) -> None:
    projection = _Projection([_row(1)])
    environ = _environ(tmp_path, with_url=False)
    environ["OMNIDASH_API_URL"] = URL
    read = _source(projection, environ).read(REQUIREMENTS, RUN_AT)
    assert read.snapshot is None
    assert projection.requests == []


def test_a_refused_or_empty_projection_is_an_error_and_never_an_empty_queue(
    tmp_path: Path,
) -> None:
    environ = _environ(tmp_path)
    refused = _source(_Projection([], status=503), environ).read(REQUIREMENTS, RUN_AT)
    assert refused.snapshot is None
    assert "not read" in (refused.error or "")
    empty = _source(_Projection([]), environ).read(REQUIREMENTS, RUN_AT)
    assert empty.snapshot is None
    assert "no rows" in (empty.error or "")


def test_a_missing_goal_scope_is_an_error(tmp_path: Path) -> None:
    environ = _environ(tmp_path)
    (tmp_path / "scope.yaml").unlink()
    projection = _Projection([_row(1)])
    read = _source(projection, environ).read(REQUIREMENTS, RUN_AT)
    assert read.snapshot is None
    assert projection.requests == []


def test_the_overlay_document_loads_through_its_model_and_names_the_address(
    tmp_path: Path,
) -> None:
    """The overlays repository pins this model for the overlay file; the address the source asks is its field."""
    from omnimarket.handlers.node_overlay_reader import load_structured_overlay
    from omnimarket.models.node_overlays.model_node_overlays import (
        ModelPrStateReadOverlay,
    )

    loaded = load_structured_overlay(
        "node_pr_state_read_effect", ModelPrStateReadOverlay, _environ(tmp_path)
    )
    assert loaded.projection_api_url == URL
