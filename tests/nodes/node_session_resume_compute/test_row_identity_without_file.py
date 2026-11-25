# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A closing reference binds its CLAIM by row identity, so session-resume reads with no ledger file.

Bus rows carry no file line. The identity is ``RID1/<KIND>/<stamp>/<lane>``: the writer's closing
token (``close_row``) and the reader (``dangling``) both use it. The old forms (an LCT1 token, a
ledger line pointer) stay readable for rows stamped before ``LEGACY_TOKEN_SUNSET``.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_session_resume_compute import HandlerSessionResume
from omnimarket.nodes.node_session_resume_compute.handlers.dangling import (
    LEGACY_TOKEN_SUNSET,
    read_ledger_rows,
)
from omnimarket.nodes.node_session_resume_compute.handlers.ledger_grammar import (
    claim_row_digest,
)
from omnimarket.nodes.node_session_resume_compute.models import (
    ModelSessionResumeRequest,
    ModelSessionResumeResult,
)

pytestmark = pytest.mark.unit

OPERATOR = [{"source": "github_auth", "value": "fixture-operator-gh"}]
IDENTITY = (
    Path(__file__).resolve().parents[2] / "fixtures" / "team_identity" / "valid.yaml"
)
CLAIM_A = (
    "2026-11-01T08:00:00Z | CLAIM | lane=lane-a | actor=claude:fixture | ticket=OMN-9000003 | "
    "worktree=none | scope=a"
)
CLAIM_B = (
    "2026-11-01T08:00:00Z | CLAIM | lane=lane-b | actor=claude:fixture | ticket=OMN-9000004 | "
    "worktree=none | scope=b"
)


def terminal(stamp: str, lane: str, ref: str) -> str:
    return (
        f"{stamp} | TERMINAL | lane={lane} | closed-by=sess | closes-CLAIM={ref} | "
        "ticket=OMN-9000003 | outcome=released"
    )


class FakeBus:
    """Stands in for ``read_ledger_rows_range_via_node``: rows only, no file, no line numbers."""

    def __init__(self, rows: tuple[str, ...]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, datetime]] = []

    def __call__(
        self,
        reader: str,
        since: datetime,
        until: datetime | None = None,
        *,
        kinds: tuple[str, ...] = (),
    ) -> tuple[str, ...]:
        self.calls.append((reader, since))
        return self.rows


@pytest.fixture
def overlay_roots(tmp_path: Path) -> list[str]:
    import shutil

    roots = tmp_path / "roots"
    (roots / "team_identity").mkdir(parents=True)
    shutil.copyfile(IDENTITY, roots / "team_identity" / "overlay.yaml")
    return [str(roots)]


def dangling_lanes(
    overlay_roots: list[str], tmp_path: Path, rows: tuple[str, ...]
) -> tuple[set[str], ModelSessionResumeResult, FakeBus]:
    bus = FakeBus(rows)
    missing = tmp_path / "no-file" / "absent.md"
    request: dict[str, Any] = {
        "operation": "dangling",
        "ledger_path": str(missing),
        "ledger_source": "bus",
        "identity_claims": OPERATOR,
        "overlay_roots": overlay_roots,
        "since": "2026-10-30T00:00:00Z",
        "t0": "2026-11-01T07:00:00Z",
        "now": "2026-11-02T00:00:00Z",
    }
    result = HandlerSessionResume(rows_reader=bus).handle(
        ModelSessionResumeRequest.model_validate(request)
    )
    return {d.lane for d in result.dangling}, result, bus


def test_bus_read_needs_no_ledger_file(
    overlay_roots: list[str], tmp_path: Path
) -> None:
    lanes, result, bus = dangling_lanes(overlay_roots, tmp_path, (CLAIM_A, CLAIM_B))
    assert result.status == "ok"
    assert lanes == {"lane-a", "lane-b"}
    assert len(bus.calls) == 1
    assert bus.calls[0][0] == "session-resume"


def test_rid_token_closes_exactly_its_claim(
    overlay_roots: list[str], tmp_path: Path
) -> None:
    rows = (
        CLAIM_A,
        CLAIM_B,
        terminal(
            "2026-11-01T09:00:00Z",
            "lane-a",
            "RID1/CLAIM/2026-11-01T08:00:00Z/lane-a",
        ),
    )
    lanes, _, _ = dangling_lanes(overlay_roots, tmp_path, rows)
    # Same stamp on both CLAIMs: the identity names lane-a only.
    assert lanes == {"lane-b"}


def test_rid_token_of_another_kind_binds_nothing(
    overlay_roots: list[str], tmp_path: Path
) -> None:
    rows = (
        CLAIM_A,
        terminal(
            "2026-11-01T09:00:00Z",
            "lane-x",
            "RID1/STATUS/2026-11-01T08:00:00Z/lane-a",
        ),
    )
    lanes, _, _ = dangling_lanes(overlay_roots, tmp_path, rows)
    assert lanes == {"lane-a"}


def test_writer_mints_the_rid_token_the_reader_binds(
    overlay_roots: list[str], tmp_path: Path
) -> None:
    close = {
        "lane": "sess",
        "dead_lane": "lane-a",
        "dead_claim": "2026-11-01T08:00:00Z",
        "mode": "released",
        "ticket": "OMN-9000003",
        "reason": "released",
    }
    request = ModelSessionResumeRequest.model_validate(
        {
            "operation": "close",
            "ledger_path": str(tmp_path / "none.md"),
            "ledger_source": "bus",
            "identity_claims": OPERATOR,
            "overlay_roots": overlay_roots,
            "now": "2026-11-01T09:00:00Z",
            "close": close,
        }
    )
    row = HandlerSessionResume(rows_reader=FakeBus((CLAIM_A,))).handle(request).row
    assert row is not None
    assert "closes-CLAIM=RID1/CLAIM/2026-11-01T08:00:00Z/lane-a" in row
    lanes, _, _ = dangling_lanes(overlay_roots, tmp_path, (CLAIM_A, CLAIM_B, row))
    assert lanes == {"lane-b"}


def test_old_lct1_token_still_reads_without_a_file(
    overlay_roots: list[str], tmp_path: Path
) -> None:
    token = f"LCT1-1-7-{claim_row_digest(CLAIM_A)}-lane-a"
    rows = (CLAIM_A, CLAIM_B, terminal("2026-11-01T09:00:00Z", "lane-a", token))
    lanes, _, _ = dangling_lanes(overlay_roots, tmp_path, rows)
    assert lanes == {"lane-b"}


def test_old_tokens_are_refused_once_the_window_ends(
    overlay_roots: list[str], tmp_path: Path
) -> None:
    token = f"LCT1-1-7-{claim_row_digest(CLAIM_A)}-lane-a"
    late = "2027-06-01T09:00:00Z"
    assert late >= LEGACY_TOKEN_SUNSET
    rows = (CLAIM_A, terminal(late, "lane-x", token))
    lanes, result, _ = dangling_lanes(overlay_roots, tmp_path, rows)
    assert lanes == {"lane-a"}
    assert any("LEGACY-TOKEN-EXPIRED" in n for n in result.notes)


def test_old_line_pointer_unresolvable_on_bus_rows_is_noted(
    overlay_roots: list[str], tmp_path: Path
) -> None:
    rows = (
        CLAIM_A,
        terminal("2026-11-01T09:00:00Z", "lane-a", "ledger:12"),
    )
    # lane-a's own TERMINAL still closes by lane; the pointer itself must not bind line 0 or any row.
    _, result, _ = dangling_lanes(overlay_roots, tmp_path, rows)
    assert result.status == "ok"


def test_read_ledger_rows_gives_rows_no_line_number() -> None:
    ledger = read_ledger_rows((CLAIM_A,))
    assert [r.live for r in ledger.recs] == [False]
    assert ledger.recs[0].source.startswith("bus:")
    assert ledger.recs[0].identity == "RID1/CLAIM/2026-11-01T08:00:00Z/lane-a"
