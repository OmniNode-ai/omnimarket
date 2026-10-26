"""The throughput tick's host pool alarm node (OMN-17427).

2026-10-21 ~16:20Z host-a read UNREADABLE(rc=255 timed out after 20s) while it swapped, and nothing said so. The tick's
end-to-end behaviour is tested in ``plugins/omni/skills/session-start/tests/test_throughput_tick.py``.
"""

from __future__ import annotations

import importlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
NODE = "omnimarket.nodes.node_host_pool_alarm_effect"
handler = importlib.import_module(f"{NODE}.handler")
models = importlib.import_module(f"{NODE}.models")
node = importlib.import_module(f"{NODE}.node")
NOW = datetime(2026, 10, 21, 16, 55, 0, tzinfo=UTC)
TIMEOUT = "rc=255 timed out after 20s"


def reading(error: str | None = None, **cells):
    return models.ModelHostPoolReading("host-a", 16, error, **cells)


def tick(tmp_path: Path, *readings, minutes: int = 0, emit: bool = True):
    request = models.ModelHostPoolAlarmRequest(
        list(readings),
        NOW + timedelta(minutes=minutes),
        tmp_path / "state",
        tmp_path / "queue",
        emit,
        plugin_repo="plugin-repo-a",
    )
    return node.NodeHostPoolAlarmEffect().handle(request)


def rows(tmp_path: Path) -> list[dict]:
    path = tmp_path / "queue" / "exception-queue.jsonl"
    return (
        [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []
    )


def test_unreadable_names_the_host_its_cap_the_error_and_a_fix_on_the_second_tick_only(
    tmp_path,
):
    assert tick(tmp_path, reading(TIMEOUT)).found == []
    out = tick(tmp_path, reading(TIMEOUT), minutes=5)
    ((level, loop, why, fix),) = out.found
    assert (level, loop) == ("missing", "host-unreadable:host-a")
    assert why.startswith(
        f"host-a (lane cap 16) pool read UNREADABLE on 2 consecutive ticks since 2026-10-21T16:55:00Z: {TIMEOUT}"
    )
    assert "read its load, memory, swap and sshd" in fix
    assert out.silenced == {"host-a"}
    (row,) = rows(tmp_path)
    assert row["decision"] is True
    assert row["cls"] == "host-unreadable"
    assert row["key"] == "host-a"


def test_the_swapping_line_carries_the_probe_fields_host_a_printed(tmp_path):
    # 2026-10-21: 74 GB swap used, swap-in about 85 MB/s, load about 40, answering the probe late
    out = tick(
        tmp_path,
        reading(
            load1=40.0,
            swap_used_frac=0.58,
            swap_in_kb_s=85000.0,
            swap_out_kb_s=900.0,
            mem_psi_avg60=25.0,
        ),
    )
    ((_, loop, why, _),) = out.found
    assert loop == "host-swapping:host-a"
    assert "swap-in 85 MB/s over 20 MB/s" in why
    assert "memory PSI avg60 25.00 over 20" in why
    assert "(load1=40.0,swap=58%,si=85000KB/s,so=900KB/s,mempsi=25.00)" in why


def test_a_host_under_the_bounds_prints_nothing(tmp_path):
    out = tick(
        tmp_path,
        reading(
            load1=4.0,
            swap_in_kb_s=19999.0,
            swap_out_kb_s=90000.0,
            mem_psi_avg60=19.9,
            mem_pressure=2,
        ),
    )
    assert out.found == []
    assert out.silenced == frozenset()
    assert rows(tmp_path) == []


def test_macos_critical_pressure_is_swapping(tmp_path):
    out = tick(tmp_path, reading(mem_pressure=4))
    assert out.found[0][1] == "host-swapping:host-a"
    assert "memory pressure level 4 (critical)" in out.found[0][2]


def test_a_host_the_table_marks_down_is_not_alarmed_and_loses_its_streak(tmp_path):
    tick(tmp_path, reading(TIMEOUT))
    assert (
        tick(
            tmp_path,
            models.ModelHostPoolReading("host-a", 16, TIMEOUT, available=False),
        ).found
        == []
    )
    assert tick(tmp_path, reading(TIMEOUT)).found == []  # the count began again


def test_a_read_only_tick_reads_the_streaks_and_writes_nothing(tmp_path):
    tick(tmp_path, reading(TIMEOUT))
    out = tick(tmp_path, reading(TIMEOUT), emit=False)
    assert [f[1] for f in out.found] == ["host-unreadable:host-a"]
    assert rows(tmp_path) == []
    assert (
        json.loads((tmp_path / "state" / handler.STATE_FILE).read_text())["hosts"][
            "host-a"
        ]["unreadable"]
        == 1
    )


def test_no_pool_read_judges_nothing_and_resets_nothing(tmp_path):
    tick(tmp_path, reading(TIMEOUT))
    assert tick(tmp_path).found == []
    assert [f[1] for f in tick(tmp_path, reading(TIMEOUT)).found] == [
        "host-unreadable:host-a"
    ]


def test_two_clean_reads_end_the_episode_and_resolve_its_row(tmp_path):
    tick(tmp_path, reading(TIMEOUT))
    tick(tmp_path, reading(TIMEOUT))
    assert tick(tmp_path, reading()).found == []  # one clean read: still the episode
    tick(tmp_path, reading())
    assert [r.get("resolves") for r in rows(tmp_path)][1:] == [rows(tmp_path)[0]["id"]]
    tick(tmp_path, reading(TIMEOUT))
    tick(tmp_path, reading(TIMEOUT))
    assert (
        len([r for r in rows(tmp_path) if "resolves" not in r]) == 2
    )  # the next episode is pushed again


def test_an_unwritable_queue_is_one_unknown_and_the_missing_line_stands(tmp_path):
    (tmp_path / "queue").write_text("a file where the queue directory belongs")
    tick(tmp_path, reading(TIMEOUT))
    found = tick(tmp_path, reading(TIMEOUT)).found
    assert [f[:2] for f in found] == [
        ("missing", "host-unreadable:host-a"),
        ("unknown", "host-pool-attention"),
    ]


def test_the_contract_names_the_handler():
    contract = yaml.safe_load(
        (
            ROOT / "src/omnimarket/nodes/node_host_pool_alarm_effect/contract.yaml"
        ).read_text()
    )
    assert contract["node_type"] == "effect"
    assert contract["handler"]["class"] == "HandlerHostPoolAlarm"
    assert "host-unreadable:<host>" in contract["description"]
    assert "host-swapping:<host>" in contract["description"]


def test_an_emitting_tick_without_a_plugin_repo_is_one_unknown_and_the_missing_line_stands(
    tmp_path,
):
    request = models.ModelHostPoolAlarmRequest(
        [reading(TIMEOUT)], NOW, tmp_path / "state", tmp_path / "queue", True
    )
    node.NodeHostPoolAlarmEffect().handle(request)
    result = node.NodeHostPoolAlarmEffect().handle(
        models.ModelHostPoolAlarmRequest(
            [reading(TIMEOUT)],
            NOW + timedelta(minutes=1),
            tmp_path / "state",
            tmp_path / "queue",
            True,
        )
    )
    assert any(
        loop == "host-pool-attention" and "plugin_repo is not set" in why
        for _l, loop, why, _f in result.found
    )
    assert any(loop.startswith("host-unreadable:") for _l, loop, _w, _f in result.found)
