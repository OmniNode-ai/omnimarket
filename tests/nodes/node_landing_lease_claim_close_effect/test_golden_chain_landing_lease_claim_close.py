# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Lease claim closure, retries, relay records, and lazy deployment tickets."""

import json
import signal
from pathlib import Path
from typing import Any

import pytest

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_landing_lease_claim_close_effect.handlers import (
    handler_landing_lease_claim_close as close,
)
from omnimarket.nodes.node_landing_lease_claim_close_effect.models import (
    ModelLeaseClaimCloseRequest,
)

pytestmark = pytest.mark.unit
CLAIMED = "2030-01-01T00:00:00Z"
RELEASED = "2030-01-01T01:00:00Z"
READ = "2030-01-01T04:00:00Z"


@pytest.fixture(autouse=True)
def no_overlay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ONEX_SKILL_OVERLAY_ROOTS", raising=False)


@pytest.fixture
def overlay(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "overlays/landing_controller"
    directory.mkdir(parents=True)
    (directory / "overlay.yaml").write_text(
        "cause_ticket: OMN-0001\nfallback_ticket: OMN-0002\n"
    )
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(directory.parent))
    return directory.parent


def claim(
    lid: int = 1, *, cause: bool = False, ticket: str = "OMN-0000"
) -> dict[str, Any]:
    return {
        "lane": f"landing-{'cause-' if cause else ''}L{lid}",
        "ts": CLAIMED,
        "ticket": ticket,
        "pr": "example#1",
    }


def release(path: Path, lid: int = 1, at: str = RELEASED) -> Path:
    directory = close.lease_dir(path, lid)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "lease.json").write_text(json.dumps({"released_at": at}))
    return directory


def request(path: Path, **changes: Any) -> ModelLeaseClaimCloseRequest:
    return ModelLeaseClaimCloseRequest.model_validate(
        {
            "claims": (claim(),),
            "live_lease_ids": frozenset(),
            "revoked_lease_ids": frozenset(),
            "released_this_tick": frozenset(),
            "read_at": READ,
            "state_dir": path,
            "writer": lambda _row: (True, ""),
            "clock": lambda: READ,
            **changes,
        }
    )


def test_released_claim_gets_one_terminal_with_its_ticket(tmp_path: Path) -> None:
    directory = release(tmp_path)
    written: list[str] = []
    req = request(tmp_path, writer=lambda row: (written.append(row) is None, ""))
    first = close.HandlerLandingLeaseClaimClose().handle(req)
    assert first.rows == tuple(written)
    assert len(written) == 1
    assert " | TERMINAL | " in written[0]
    assert "ticket=OMN-0000" in written[0]
    assert "pr=example#1" in written[0]
    assert first.closed == ("landing-L1",)
    assert first.failed == ()
    assert (
        json.loads((directory / close.LEDGER_CLOSE_FILE).read_text())["landing-L1"][
            "ok"
        ]
        is True
    )
    again = close.HandlerLandingLeaseClaimClose().handle(req)
    assert again.rows == ()
    assert again.skipped_closed == 1
    assert len(written) == 1
    assert (
        first.line()
        == "LEASE-CLAIM-CLOSE closed=1 failed=0 skipped_live=0 skipped_recent=0 left=0 skipped_closed=0"
    )


@pytest.mark.parametrize(
    "mode", ["live", "released_this_tick", "at_read", "after_read"]
)
def test_live_or_recently_released_is_skipped(tmp_path: Path, mode: str) -> None:
    release(
        tmp_path,
        at=READ
        if mode == "at_read"
        else "2030-01-01T05:00:00Z"
        if mode == "after_read"
        else RELEASED,
    )
    kwargs = (
        {"live_lease_ids": {1}}
        if mode == "live"
        else {"released_this_tick": {1}}
        if mode == "released_this_tick"
        else {}
    )
    result = close.HandlerLandingLeaseClaimClose().handle(request(tmp_path, **kwargs))
    assert result.rows == ()
    assert result.skipped_live == 1


@pytest.mark.parametrize("has_lease", [False, True])
def test_claim_without_release_is_held_for_three_hours(
    tmp_path: Path, has_lease: bool
) -> None:
    if has_lease:
        release(tmp_path, at="")
    recent = {**claim(), "ts": "2030-01-01T02:00:00Z"}
    result = close.HandlerLandingLeaseClaimClose().handle(
        request(tmp_path, claims=(recent,))
    )
    assert result.rows == ()
    assert result.skipped_recent == 1
    old = {**recent, "ts": "2030-01-01T01:00:00Z"}
    result = close.HandlerLandingLeaseClaimClose().handle(
        request(tmp_path, claims=(old,))
    )
    assert result.closed == ("landing-L1",)


def test_existing_ok_record_is_skipped(tmp_path: Path) -> None:
    directory = release(tmp_path)
    (directory / close.LEDGER_CLOSE_FILE).write_text(
        json.dumps({"landing-L1": {"ok": True}})
    )
    result = close.HandlerLandingLeaseClaimClose().handle(request(tmp_path))
    assert result.rows == ()
    assert result.skipped_closed == 1


def test_refused_write_is_recorded_and_retried(tmp_path: Path) -> None:
    directory = release(tmp_path)
    answers = iter([(False, "refused"), (True, "")])
    written: list[str] = []

    def writer(row: str) -> tuple[bool, str]:
        written.append(row)
        return next(answers)

    req = request(tmp_path, writer=writer)
    first = close.HandlerLandingLeaseClaimClose().handle(req)
    assert first.failed == ("landing-L1",)
    assert first.closed == ()
    assert (
        json.loads((directory / close.LEDGER_CLOSE_FILE).read_text())["landing-L1"][
            "error"
        ]
        == "refused"
    )
    assert close.HandlerLandingLeaseClaimClose().handle(req).closed == ("landing-L1",)
    assert close.HandlerLandingLeaseClaimClose().handle(req).skipped_closed == 1
    assert len(written) == 2


@pytest.mark.parametrize(
    ("limit", "count", "left"), [(1, 1, 2), (0, 0, 3), (-1, 0, 3), (None, 3, 0)]
)
def test_limit_and_left(
    tmp_path: Path, limit: int | None, count: int, left: int
) -> None:
    claims = tuple(claim(i) for i in range(1, 4))
    for i in range(1, 4):
        release(tmp_path, i)
    result = close.HandlerLandingLeaseClaimClose().handle(
        request(tmp_path, claims=claims, limit=limit)
    )
    assert len(result.rows) == count
    assert result.left == left


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    def refuse_write(row: str) -> tuple[bool, str]:
        pytest.fail("dry run called the writer")

    result = close.HandlerLandingLeaseClaimClose().handle(
        request(tmp_path, dry_run=True, writer=refuse_write)
    )
    assert result.closed == ("landing-L1",)
    assert len(result.rows) == 1
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    ("cause", "setting"), [(False, "fallback_ticket"), (True, "cause_ticket")]
)
def test_missing_ticket_requires_overlay(
    tmp_path: Path, cause: bool, setting: str
) -> None:
    with pytest.raises(NodeOverlayError, match=setting):
        close.HandlerLandingLeaseClaimClose().handle(
            request(tmp_path, claims=(claim(cause=cause, ticket=""),))
        )
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(("cause", "ticket"), [(False, "OMN-0002"), (True, "OMN-0001")])
def test_overlay_supplies_ticket(
    tmp_path: Path, overlay: Path, cause: bool, ticket: str
) -> None:
    result = close.HandlerLandingLeaseClaimClose().handle(
        request(tmp_path, claims=(claim(cause=cause, ticket=""),))
    )
    assert f"ticket={ticket}" in result.rows[0]
    assert close.cause_ticket({"ONEX_SKILL_OVERLAY_ROOTS": str(overlay)}) == "OMN-0001"
    assert (
        close.fallback_ticket({"ONEX_SKILL_OVERLAY_ROOTS": str(overlay)}) == "OMN-0002"
    )


@pytest.mark.parametrize("cause", [False, True])
@pytest.mark.parametrize("configured", [False, True])
def test_explicit_request_tickets_win(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cause: bool, configured: bool
) -> None:
    if configured:
        root = tmp_path / "overlays/landing_controller"
        root.mkdir(parents=True)
        (root / "overlay.yaml").write_text(
            "cause_ticket: OMN-0001\nfallback_ticket: OMN-0002\n"
        )
        monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(root.parent))
    result = close.HandlerLandingLeaseClaimClose().handle(
        request(
            tmp_path,
            claims=(claim(cause=cause, ticket=""),),
            cause_ticket="OMN-0003",
            fallback_ticket="OMN-0004",
        )
    )
    assert f"ticket={'OMN-0003' if cause else 'OMN-0004'}" in result.rows[0]


def test_claim_ticket_takes_precedence_over_explicit_values(tmp_path: Path) -> None:
    result = close.HandlerLandingLeaseClaimClose().handle(
        request(tmp_path, cause_ticket="OMN-0003", fallback_ticket="OMN-0004")
    )
    assert "ticket=OMN-0000" in result.rows[0]


def test_cause_claim_and_terminal_use_separate_relay_once(
    tmp_path: Path, overlay: Path
) -> None:
    directory = close.lease_dir(tmp_path, 1)
    written: list[str] = []

    def writer(row: str) -> tuple[bool, str]:
        written.append(row)
        return True, ""

    action = {
        "lease_id": 1,
        "subject": "cause:example/repo:group",
        "cause_brief": {"pairs": [{"check": "unit"}], "members": ["example#1"]},
    }
    assert close.cause_claim(directory, action, "base", writer, lambda: CLAIMED) == (
        True,
        "",
    )
    close.write_relay(directory, {"lane": "landing-L1", "terminal": {"ok": True}})
    (directory / "result.json").write_text(
        json.dumps({"kind": "fixed", "reason": "complete"})
    )
    close.cause_terminal(directory, writer, lambda: RELEASED)
    close.cause_terminal(directory, writer, lambda: READ)
    assert len(written) == 2
    assert " | CLAIM | " in written[0]
    assert " | TERMINAL | " in written[1]
    assert all("ticket=OMN-0001" in row and "base=base" in row for row in written)
    relay = json.loads((directory / close.CAUSE_RELAY_FILE).read_text())
    assert relay["lane"] == "landing-cause-L1"
    assert relay["terminal"]["ok"] is True
    result = close.HandlerLandingLeaseClaimClose().handle(
        request(tmp_path, claims=(claim(cause=True),))
    )
    assert result.skipped_closed == 1
    assert result.rows == ()


def test_cause_ticket_is_lazy_until_a_row_is_needed(tmp_path: Path) -> None:
    close.cause_terminal(tmp_path, lambda _row: (True, ""))
    with pytest.raises(NodeOverlayError, match="cause_ticket"):
        close.cause_claim(
            tmp_path,
            {"lease_id": 1, "subject": "cause:example/repo:group"},
            "",
            lambda _row: (True, ""),
        )


def test_cause_terminal_refusal_retries(tmp_path: Path, overlay: Path) -> None:
    close.cause_claim(
        tmp_path,
        {"lease_id": 1, "subject": "cause:example/repo:group"},
        "",
        lambda _row: (True, ""),
        lambda: CLAIMED,
    )
    close.cause_terminal(tmp_path, lambda _row: (False, "refused"), lambda: RELEASED)
    relay = json.loads((tmp_path / close.CAUSE_RELAY_FILE).read_text())
    assert relay["terminal"] == {"ts": RELEASED, "ok": False, "error": "refused"}
    close.cause_terminal(tmp_path, lambda _row: (True, ""), lambda: READ)
    assert (
        json.loads((tmp_path / close.CAUSE_RELAY_FILE).read_text())["terminal"]["ok"]
        is True
    )


def test_invalid_read_stamp_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="ledger read time"):
        close.HandlerLandingLeaseClaimClose().handle(request(tmp_path, read_at="bad"))


def test_signal_helpers_restore_handlers_and_parser_is_standalone() -> None:
    previous = close.kill_exits()
    try:
        installed = signal.getsignal(signal.SIGTERM)
        assert callable(installed)
        with pytest.raises(SystemExit) as exc:
            installed(signal.SIGTERM, None)
        assert exc.value.code == 128 + signal.SIGTERM
        assert signal.getsignal(signal.SIGINT) == signal.SIG_IGN
    finally:
        close.restore_signals(previous)
    assert signal.getsignal(signal.SIGTERM) == previous[signal.SIGTERM]
    args = close.backfill_parser().parse_args(["--dry-run", "--limit", "2"])
    assert args.dry_run
    assert args.limit == 2
