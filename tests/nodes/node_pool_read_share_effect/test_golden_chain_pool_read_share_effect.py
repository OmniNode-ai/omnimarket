# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pool cache cases from test_placement_lock_pool_read.py, invoked locally."""

from __future__ import annotations

import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

import omnimarket.nodes.node_pool_read_share_effect as share_node
from omnimarket.nodes.node_pool_read_share_effect.handlers import (
    handler_pool_read_share as share,
)
from omnimarket.nodes.node_pool_read_share_effect.models import (
    model_pool_read_share as share_models,
)

pytestmark = pytest.mark.unit


@dataclass(frozen=True)
class Reading:
    host: SimpleNamespace
    placed: int
    fresh: int = 0


class CountingCapture:
    def __init__(self, delay_s: float = 0.0, returncode: int = 0) -> None:
        self.calls: list[list[str]] = []
        self.delay_s, self.returncode = delay_s, returncode
        self._mutex = threading.Lock()

    def __call__(
        self, argv: list[str], timeout: float = 20.0
    ) -> subprocess.CompletedProcess[str]:
        with self._mutex:
            self.calls.append(list(argv))
            n = len(self.calls)
        time.sleep(self.delay_s)
        return subprocess.CompletedProcess(argv, self.returncode, f"answer {n}", "")


def test_concurrent_placements_share_one_capture_of_a_probe(tmp_path: Path) -> None:
    inner = CountingCapture(delay_s=0.5)
    outs: list[str] = []

    def ask(lane: str) -> None:
        shared = share.shared(tmp_path, inner)
        outs.append(shared(["sh", f"# lane={lane} placement-probe\necho probe"]).stdout)

    threads = [threading.Thread(target=ask, args=(f"lane-{i}",)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(inner.calls) == 1, (
        "the lane line is the reader's, not the probe's: eight placements, one probe"
    )
    assert outs == ["answer 1"] * 8


def test_an_old_answer_is_captured_again_and_a_failure_is_shared(
    tmp_path: Path,
) -> None:
    now = [1000.0]
    inner = CountingCapture(returncode=255)
    shared = share.shared(tmp_path, inner, ttl_s=30.0, clock=lambda: now[0])
    first = shared(["ping", "-c", "50", "probe-a"])
    assert first.returncode == 255
    assert len(inner.calls) == 1
    now[0] += 29.0
    assert shared(["ping", "-c", "50", "probe-a"]).returncode == 255
    assert len(inner.calls) == 1, "a timed-out host is not re-paid"
    now[0] += 2.0
    assert shared(["ping", "-c", "50", "probe-a"]).stdout == "answer 2"
    assert len(inner.calls) == 2
    shared(["ping", "-c", "50", "probe-b"])
    assert len(inner.calls) == 3, "another probe is its own answer"


def test_the_window_is_shorter_than_the_link_history_gap() -> None:
    # The link-history component is outside this port; its source gap is 60 seconds.
    link_read_min_gap_s = 60.0
    assert link_read_min_gap_s > share.POOL_READ_TTL_S, (
        "one shared ping must never count as two link reads"
    )
    assert share.POOL_READ_DIR.startswith("."), (
        "placed_counts skips dot names; a marker-shaped name would count as a unit"
    )


def test_the_node_says_when_an_answer_was_shared(tmp_path: Path) -> None:
    node = share_node.NodePoolReadShareEffect(CountingCapture())
    request = share_models.ModelPoolReadShareRequest(
        directory=str(tmp_path), argv=("ping", "probe-a"), timeout_s=20.0, ttl_s=30.0
    )
    assert (node.handle(request).shared, node.handle(request).shared) == (False, True)
    assert (Path(share_node.__file__).parent / "contract.yaml").is_file()


def test_recount_takes_the_markers_as_they_stand(tmp_path: Path) -> None:
    stale = [Reading(host=SimpleNamespace(name="pool-member"), placed=0)]
    (now,) = share.recount(stale, {"pool-member": 2}, {"pool-member": 5})
    assert (now.placed, now.fresh) == (2, 2)
    (now,) = share.recount(stale, {})
    assert (now.placed, now.fresh) == (0, 0)


def test_probe_key_ignores_only_the_lane_header() -> None:
    assert share.probe_key(["sh", "# lane=one\necho probe"]) == share.probe_key(
        ["sh", "# lane=two\necho probe"]
    )
    assert share.probe_key(["sh", "echo other"]) != share.probe_key(
        ["sh", "echo probe"]
    )


def test_unwritable_cache_falls_back_to_capture(tmp_path: Path) -> None:
    tmp_path.joinpath("file").write_text("occupied")
    capture = CountingCapture()
    run = share.shared(tmp_path / "file", capture)
    assert run(["probe"]).stdout == "answer 1"
    assert run(["probe"]).stdout == "answer 2"


def test_default_capture_runs_the_probe_live(tmp_path: Path) -> None:
    """With no capture given the handler runs the probe itself; a probe that cannot start is exit 255."""
    handler = share.HandlerPoolReadShare()
    done = handler.handle(
        share_models.ModelPoolReadShareRequest(
            directory=str(tmp_path / "share"),
            argv=("sh", "-c", "printf live"),
            timeout_s=10.0,
            ttl_s=30.0,
        )
    )
    assert (done.returncode, done.stdout, done.shared) == (0, "live", False)
    missing = handler.handle(
        share_models.ModelPoolReadShareRequest(
            directory=str(tmp_path / "share"),
            argv=(str(tmp_path / "no-such-probe"),),
            timeout_s=10.0,
            ttl_s=30.0,
        )
    )
    assert missing.returncode == 255
    assert missing.stderr


FAKE_REMOTE_TWO_STEPS_THEN_HANG = (
    "echo 'PROBE-STEP capacity'; echo 'PROBE-STEP claude-login'; sleep 30"
)


def test_a_probe_that_times_out_names_the_step_it_stalled_in() -> None:
    done = share.capture_live(
        ["/bin/sh", "-c", FAKE_REMOTE_TWO_STEPS_THEN_HANG], timeout=1.0
    )
    assert done.returncode == 255
    assert done.stderr == "timed out after 1s in step claude-login"
    assert done.stdout == "PROBE-STEP capacity\nPROBE-STEP claude-login\n", (
        "the partial stdout is kept, not thrown away"
    )


def test_a_probe_that_times_out_before_any_marker_says_so() -> None:
    done = share.capture_live(["/bin/sh", "-c", "sleep 30"], timeout=1.0)
    assert (done.returncode, done.stdout) == (255, "")
    assert done.stderr == "timed out after 1s before any step marker"


def test_a_shared_read_replays_the_step_a_timeout_named(tmp_path: Path) -> None:
    now = [1000.0]
    shared = share.shared(
        tmp_path, share.capture_live, ttl_s=30.0, clock=lambda: now[0]
    )
    argv = ["/bin/sh", "-c", FAKE_REMOTE_TWO_STEPS_THEN_HANG]
    first = shared(argv, timeout=1.0)
    now[0] += 5.0
    replay = shared(argv, timeout=1.0)
    assert replay.returncode == 255
    assert replay.stderr == first.stderr == "timed out after 1s in step claude-login"


def test_the_pool_alarm_row_carries_the_step(tmp_path: Path) -> None:
    from datetime import UTC, datetime, timedelta

    from omnimarket.nodes.node_host_pool_alarm_effect.models import (
        ModelHostPoolAlarmRequest,
        ModelHostPoolReading,
    )
    from omnimarket.nodes.node_host_pool_alarm_effect.node import (
        NodeHostPoolAlarmEffect,
    )

    done = share.capture_live(
        ["/bin/sh", "-c", FAKE_REMOTE_TWO_STEPS_THEN_HANG], timeout=1.0
    )
    # read_pool's failed-read text: ``rc=<code> <stderr or stdout>``
    error = f"rc={done.returncode} {(done.stderr or done.stdout).strip()[:100]}"
    now = datetime(2026, 10, 26, 2, 23, tzinfo=UTC)
    found: list[tuple[str, str, str, str]] = []
    for minute in (0, 5):
        found = (
            NodeHostPoolAlarmEffect()
            .handle(
                ModelHostPoolAlarmRequest(
                    [ModelHostPoolReading("h105", 16, error)],
                    now + timedelta(minutes=minute),
                    tmp_path / "state",
                    tmp_path / "queue",
                    True,
                    plugin_repo="plugin-repo-a",
                )
            )
            .found
        )
    ((_, loop, why, _),) = found
    assert loop == "host-unreadable:h105"
    assert "UNREADABLE" in why
    assert "timed out after 1s in step claude-login" in why


def test_a_marker_cut_off_before_its_newline_is_not_named() -> None:
    assert share.last_step("PROBE-STEP capacity\nPROBE-STEP claude-lo") == "capacity"
    assert share.last_step("PROBE 4 0.1 2048\n") is None
