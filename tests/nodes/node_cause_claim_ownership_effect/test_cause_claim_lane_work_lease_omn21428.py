# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Lane-work leases keep silent cause and PR claim owners live (OMN-21428)."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from omnimarket.nodes.node_cause_claim_ownership_effect import handler

pytestmark = pytest.mark.unit

NOW = datetime(2026, 10, 26, 6, tzinfo=UTC)
STAMP = "2026-10-26T04:00:00Z"
CAUSE = f"{STAMP} | CLAIM | lane=L1 | cause=omniclaude:Lint"
LEASE = (
    f"{STAMP} | CLAIM | lane=L1 | actor=script:node_projection_lease | "
    "kind=lane-work | lease=6f1f0c1e-8d0e-4e55-9b51-3c7a0b0f2a11 | ticket=OMN-21428"
)


def _row(rtype: str, text: str, stamp: str = STAMP) -> SimpleNamespace:
    return SimpleNamespace(rtype=rtype, lane="L1", text=text, ts=stamp)


def _qs(
    rows: list[SimpleNamespace], owners: list[SimpleNamespace] | None = None
) -> SimpleNamespace:
    dm = SimpleNamespace(
        load_grammar=lambda _root: {},
        load_rows=lambda *_args, **_kwargs: (rows, None),
        open_claims=lambda *_args: [r for r in rows if r.rtype == "CLAIM"],
        ledger_owners=lambda *_args, **_kwargs: owners or [],
        PR=lambda repo, number: SimpleNamespace(repo=repo, number=number),
        DEAD_AFTER=timedelta(minutes=45),
        CHAIN_DEAD_AFTER=timedelta(minutes=180),
    )
    return SimpleNamespace(
        load_drain_map_module=lambda: dm,
        owner_context=lambda rows, _dm: {
            "last_row": {r.lane: r.ts for r in rows},
            "dispatchers": set(),
        },
        parse_stamp=datetime.fromisoformat,
        SILENT_AFTER=timedelta(minutes=45),
    )


def test_silent_cause_claim_with_lane_work_lease_is_live(tmp_path: Path) -> None:
    qs = _qs([_row("CLAIM", CAUSE), _row("CLAIM", LEASE)])

    assert handler.live_cause_claims(qs, tmp_path, NOW, None) == [("L1", CAUSE)]


def test_terminal_ends_silent_cause_claim_lane_work_lease(tmp_path: Path) -> None:
    qs = _qs(
        [
            _row("CLAIM", CAUSE),
            _row("CLAIM", LEASE),
            _row(
                "TERMINAL",
                "2026-10-26T04:10:00Z | TERMINAL | lane=L1",
                "2026-10-26T04:10:00Z",
            ),
        ]
    )

    assert handler.live_cause_claims(qs, tmp_path, NOW, None) == []


@pytest.mark.parametrize("has_lease", [True, False])
def test_silent_pr_claim_owner_needs_lane_work_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, has_lease: bool
) -> None:
    monkeypatch.setenv("ONEX_REGISTRY_ROOT", str(tmp_path))
    rows = [_row("CLAIM", f"{STAMP} | CLAIM | lane=L1 | pr=omnimarket#42")]
    if has_lease:
        rows.append(_row("CLAIM", LEASE))
    qs = _qs(
        rows,
        [SimpleNamespace(kind="CLAIM", live=False, ended=False, lane="L1")],
    )

    assert handler.claim_lane_owners(qs, None, NOW, ["omnimarket#42"]) == {
        "omnimarket#42": ["live CLAIM L1"] if has_lease else []
    }
