# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The routes stage and the routes block of the delegation tracker (OMN-23381, AC3).

The floor shows, per hour, the delegations by route (local, GLM, other) and the local share. A reading that was not made, cannot
be parsed or is older than its declared maximum age is never drawn as a number: its state is ``unread`` or ``stale`` and it has no share.
"""

from __future__ import annotations

import json
import shlex
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_factory_floor_publish_effect.handlers.handler_factory_floor_publish import (
    NODE_DIR,
)
from omnimarket.nodes.node_floor_delegation_effect.handlers.adapter_delegation_routes import (
    ROUTES_MARKER,
)
from omnimarket.nodes.node_floor_delegation_effect.handlers.handler_floor_delegation import (
    HandlerFloorDelegation,
)
from omnimarket.nodes.node_floor_delegation_effect.models.model_floor_delegation import (
    ModelFloorDelegationRequest,
)

from .support import NOW, RT, iso, runtime, write_home

HOST_ENV = "EXAMPLE_ROUTES_SSH_HOST"
ENVIRON = {HOST_ENV: "reader@db.example.invalid"}
# Hours 18:00 to 20:00 UTC on the clock day: (local, glm, other); the last hour is the fixture the lab read (5, 128, 0).
HOURS = [("18:00", 0, 11, 0), ("19:00", 3, 119, 1), ("20:00", 5, 128, 0)]


def _line(hour: str, local: int, glm: int, other: int) -> str:
    return json.dumps(
        {
            "hour": f"2026-11-08T{hour}:00Z",
            "local": local,
            "glm": glm,
            "other": other,
            "total": local + glm + other,
        }
    )


GOOD = ROUTES_MARKER + "\n" + "\n".join(_line(*h) for h in HOURS) + "\n"


class RoutesRunner:
    """Answers the one routes read with a planted answer and records the command it was given."""

    def __init__(self, answer: tuple[int, str, str] = (0, GOOD, "")) -> None:
        self.answer = answer
        self.calls: list[tuple[Sequence[str], int]] = []

    def __call__(self, argv: Sequence[str], timeout: int) -> tuple[int, str, str]:
        self.calls.append((argv, timeout))
        return self.answer


def _handler(
    runner: RoutesRunner, *, clock_offset: timedelta = timedelta(0), environ: Any = None
) -> HandlerFloorDelegation:
    return HandlerFloorDelegation(
        RT,
        runner,
        clock=lambda: NOW + clock_offset,
        environ=ENVIRON if environ is None else environ,
    )


def _stage(handler: HandlerFloorDelegation, home: Path, stage: str) -> Any:
    return handler.handle(
        ModelFloorDelegationRequest(stage=stage, home=str(home))  # type: ignore[arg-type]
    )


def _tracker(home: Path) -> dict[str, Any]:
    return json.loads((home / RT.output_relpath).read_text(encoding="utf-8"))


def _read_and_build(tmp_path: Path, runner: RoutesRunner, **kw: Any) -> dict[str, Any]:
    home = write_home(tmp_path)
    _stage(_handler(runner, **kw), home, "routes")
    _stage(_handler(RoutesRunner(), **kw), home, "build")
    return _tracker(home)


def check_routes_block(doc: dict[str, Any]) -> dict[str, Any]:
    """What the served tracker must carry: a routes block with a state, the declared maximum age and the hourly rows."""
    assert "routes" in doc, "the tracker has no routes block"
    block = doc["routes"]
    assert block["state"] in ("fresh", "stale", "unread")
    assert block["max_age_min"] == RT.routes_max_age_min
    assert isinstance(block["hours"], list)
    return block  # type: ignore[no-any-return]


def test_golden_hourly_rows_in_the_floor_shares_out(tmp_path: Path) -> None:
    """Rows in, per-hour shares out: the last hour reads local 5, GLM 128, other 0, a local share of 5/133."""
    home = write_home(tmp_path)
    runner = RoutesRunner()
    done = _stage(_handler(runner), home, "routes")
    assert done.ok
    assert done.stage == "routes"
    saved = json.loads((home / RT.route_reads_relpath).read_text(encoding="utf-8"))
    assert saved["read_at"] == iso(NOW)
    assert [(r["hour"], r["local"], r["glm"], r["other"]) for r in saved["hours"]] == [
        (f"2026-11-08T{h}:00Z", lo, g, o) for h, lo, g, o in HOURS
    ]
    _stage(_handler(RoutesRunner()), home, "build")
    block = check_routes_block(_tracker(home))
    assert block["state"] == "fresh"
    assert block["max_age_min"] == 90
    assert block["read_at"] == iso(NOW)
    rows = block["hours"]
    assert [r["total"] for r in rows] == [11, 123, 133]
    assert rows[0]["local_share"] == pytest.approx(0.0)
    assert rows[1]["local_share"] == pytest.approx(3 / 123)
    last = block["last_hour"]
    assert (last["hour"], last["local"], last["glm"], last["other"], last["total"]) == (
        "2026-11-08T20:00:00Z",
        5,
        128,
        0,
        133,
    )
    assert last["local_share"] == pytest.approx(5 / 133)


def test_the_read_is_one_read_only_command_built_from_the_overlay(
    tmp_path: Path,
) -> None:
    """The host variable, the compose project, the service, the database, the user, the prefixes and the window all come from the overlay."""
    home = write_home(tmp_path)
    runner = RoutesRunner()
    _stage(_handler(runner), home, "routes")
    assert len(runner.calls) == 1
    argv, timeout = runner.calls[0]
    assert timeout == RT.routes_read_timeout_seconds
    assert argv[0] == "ssh"
    assert ENVIRON[HOST_ENV] in argv
    assert argv[-1].startswith("bash -c ")
    script = shlex.split(argv[-1])[2]
    assert "BEGIN READ ONLY" in script
    r = RT.routes
    assert r is not None
    for fact in (
        r.compose_project,
        r.service,
        r.database,
        r.db_user,
        r.local_prefix,
        r.glm_prefix,
        RT.routes_table,
        str(r.window_hours - 1),
    ):
        assert fact in script, fact


def test_the_prefixes_and_the_window_are_not_literals_of_the_code(
    tmp_path: Path,
) -> None:
    other = RT.routes.model_copy(  # type: ignore[union-attr]
        update={"local_prefix": "edge-", "glm_prefix": "other:glm", "window_hours": 3}
    )
    runner = RoutesRunner()
    handler = HandlerFloorDelegation(
        runtime(routes=other), runner, clock=lambda: NOW, environ=ENVIRON
    )
    _stage(handler, write_home(tmp_path), "routes")
    script = shlex.split(runner.calls[0][0][-1])[2]
    assert "edge-" in script
    assert "other:glm" in script
    assert "interval '2 hours'" in script
    assert RT.routes.local_prefix not in script  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("answer", "environ", "why"),
    [
        ((255, "", "ssh: connect to host failed"), ENVIRON, "exit 255"),
        ((124, "", "timed out after 12 s"), ENVIRON, "exit 124"),
        ((0, "not json at all\n", ""), ENVIRON, "unparsable"),
        ((0, "", ""), ENVIRON, "read marker"),
        (
            (
                0,
                ROUTES_MARKER
                + "\n"
                + json.dumps(
                    {"hour": "x", "local": 1, "glm": 1, "other": 1, "total": 9}
                ),
                "",
            ),
            ENVIRON,
            "unparsable",
        ),
        ((0, GOOD, ""), {}, HOST_ENV),
    ],
)
def test_error_chain_a_read_that_fails_is_unread_with_the_reason_and_no_share(
    tmp_path: Path, answer: tuple[int, str, str], environ: dict[str, str], why: str
) -> None:
    runner = RoutesRunner(answer)
    # A planted-empty environ must stay empty, so the handler gets a mapping, not the process environment.
    doc = _read_and_build(tmp_path, runner, environ=environ or {"UNRELATED": "1"})
    block = check_routes_block(doc)
    assert block["state"] == "unread"
    assert why in block["reason"]
    assert block["hours"] == []
    assert block["last_hour"] is None
    assert block["local_share"] is None
    if not environ:
        assert runner.calls == []


def test_error_chain_a_missing_or_broken_file_is_unread_and_no_share(
    tmp_path: Path,
) -> None:
    home = write_home(tmp_path)
    _stage(_handler(RoutesRunner()), home, "build")
    missing = check_routes_block(_tracker(home))
    assert missing["state"] == "unread"
    assert missing["last_hour"] is None
    assert missing["local_share"] is None
    target = home / RT.route_reads_relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{ truncated", encoding="utf-8")
    _stage(_handler(RoutesRunner()), home, "build")
    broken = check_routes_block(_tracker(home))
    assert broken["state"] == "unread"
    assert "not valid JSON" in broken["reason"]
    assert broken["hours"] == []
    assert broken["local_share"] is None


def test_error_chain_a_read_older_than_the_declared_age_is_stale_and_no_share(
    tmp_path: Path,
) -> None:
    home = write_home(tmp_path)
    _stage(_handler(RoutesRunner()), home, "routes")
    just_inside = timedelta(minutes=RT.routes_max_age_min)
    _stage(_handler(RoutesRunner(), clock_offset=just_inside), home, "build")
    assert check_routes_block(_tracker(home))["state"] == "fresh"
    too_old = timedelta(minutes=RT.routes_max_age_min, seconds=1)
    _stage(_handler(RoutesRunner(), clock_offset=too_old), home, "build")
    block = check_routes_block(_tracker(home))
    assert block["state"] == "stale"
    assert block["hours"] == []
    assert block["last_hour"] is None
    assert block["local_share"] is None
    assert block["read_at"] == iso(NOW)
    assert "91" in block["reason"] or "older" in block["reason"]


def test_a_quiet_window_is_fresh_with_no_share_not_zero(tmp_path: Path) -> None:
    """An answer with the marker and no rows is a window with no delegations: fresh, and the last hour has no share."""
    doc = _read_and_build(tmp_path, RoutesRunner((0, ROUTES_MARKER + "\n", "")))
    block = check_routes_block(doc)
    assert block["state"] == "fresh"
    assert block["hours"] == []
    assert block["last_hour"]["total"] == 0
    assert block["last_hour"]["local_share"] is None
    assert block["local_share"] is None


def test_failing_control_a_tracker_without_the_routes_block_fails_the_check(
    tmp_path: Path,
) -> None:
    """The check passes on the real tracker and fails when the routes block is taken out of it."""
    doc = _read_and_build(tmp_path, RoutesRunner())
    check_routes_block(doc)
    without = {k: v for k, v in doc.items() if k != "routes"}
    with pytest.raises(AssertionError, match="no routes block"):
        check_routes_block(without)


def test_the_run_stage_reads_the_routes_before_the_scenarios(tmp_path: Path) -> None:
    home = write_home(tmp_path)
    runner = RoutesRunner()
    done = _stage(_handler(runner), home, "run")
    assert done.ok
    assert (home / RT.route_reads_relpath).is_file()
    assert check_routes_block(_tracker(home))["state"] == "fresh"
    assert runner.calls[0][0][0] == "ssh"


def test_the_contract_declares_the_routes_file_and_the_age_and_fits_the_step() -> None:
    assert RT.routes_max_age_min == 90
    assert RT.route_reads_relpath == ".onex_state/floor-delegation/route-reads.json"
    assert RT.scenario_stage_deadline_seconds + RT.routes_read_timeout_seconds + 5 <= 90


def test_the_delegation_page_shows_the_routes_block() -> None:
    page = (NODE_DIR / "page" / "delegation.html").read_text(encoding="utf-8")
    for needle in ('id="routes"', "doc.routes", "local_share", "max_age_min", "unread"):
        assert needle in page, needle
