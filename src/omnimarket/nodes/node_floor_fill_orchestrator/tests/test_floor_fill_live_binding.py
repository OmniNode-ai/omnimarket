# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The live profile: the adapters, the settings, the entry and a tick over the live binding with scripted reads.

The reads (floor URL, plan files, ledger file) are the real adapters pointed at temporary files or driven by an
injected fetch; the store is the in-memory conditional store. What is not bound live (pool, tickets, brake, stop
signal) must fail the pass closed naming the seam: ``test_unbound_pool_is_not_read_as_zero_slots`` is the failing
control for that rule.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from omnibase_spi.protocols.lane import ProtocolFactSource, ProtocolWorkLedgerStore

from omnimarket.nodes.node_floor_fill_orchestrator.handlers.adapter_floor_facts_live import (
    LiveFloorFacts,
    fetch_url_text,
    read_text_file,
)
from omnimarket.nodes.node_floor_fill_orchestrator.handlers.adapter_ledger_file import (
    FileWorkLedgerStore,
)
from omnimarket.nodes.node_floor_fill_orchestrator.models import (
    EXIT_CODE_BY_STATUS,
    EnumFloorFillStatus,
    FloorFillConfigError,
)
from omnimarket.nodes.node_floor_fill_orchestrator.models.model_floor_fill_live_settings import (
    load_floor_fill_live_settings,
)
from omnimarket.nodes.node_floor_fill_orchestrator.orchestration.adapters import (
    LedgerRowsFloorLedger,
)
from omnimarket.nodes.node_floor_fill_orchestrator.orchestration.ports import (
    FloorFillUnreadableError,
)
from omnimarket.nodes.node_floor_fill_orchestrator.orchestration.scripted_ports import (
    floor_record,
)
from omnimarket.nodes.node_floor_fill_orchestrator.orchestration.store import (
    InMemoryFloorStateStore,
)
from omnimarket.nodes.node_floor_fill_orchestrator.runtime_floor_fill_orchestrator import (
    main,
    run_entry,
)
from omnimarket.nodes.node_floor_fill_orchestrator.tests.support import (
    LINES4,
    OVERLAY,
    T0,
)

FAILED = EXIT_CODE_BY_STATUS[EnumFloorFillStatus.FAILED]
DEGRADED = EXIT_CODE_BY_STATUS[EnumFloorFillStatus.DEGRADED]


def live_section(tmp_path: Path, **changes: Any) -> dict[str, Any]:
    section: dict[str, Any] = {
        "store_dsn_env": "EXAMPLE_FLOOR_FILL_DSN",
        "store_schema_name": "public",
        "store_table_name": "floor_fill_state",
        "store_connect_timeout_seconds": 5,
        "floor_fetch_timeout_seconds": 5.0,
        "plan_file_pattern": "{line_id}.md",
        "ledger_path": str(tmp_path / "ledger.md"),
        "process_output_dir": str(tmp_path / "out"),
    }
    return {**section, **changes}


def full_overlay(tmp_path: Path, **changes: Any) -> dict[str, Any]:
    return {
        "floor_fill": OVERLAY,
        "floor_fill_live": live_section(tmp_path, **changes),
        "lab_hosts": [{"name": OVERLAY["host_id"], "available": True}],
    }


def facts(tmp_path: Path, **kwargs: Any) -> LiveFloorFacts:
    return LiveFloorFacts(
        fetch_text=kwargs.pop("fetch_text", lambda _url, _timeout: "{}"),
        read_file=kwargs.pop("read_file", read_text_file),
        timeout_seconds=5.0,
        plan_file_pattern="{line_id}.md",
        brake=kwargs.pop("brake", None),
        stop=kwargs.pop("stop", None),
    )


def test_settings_missing_key_names_it(tmp_path: Path) -> None:
    section = live_section(tmp_path)
    del section["ledger_path"]
    with pytest.raises(FloorFillConfigError) as refused:
        load_floor_fill_live_settings(section)
    assert "floor_fill_live.ledger_path" in refused.value.keys


def test_settings_pattern_without_line_id_refused(tmp_path: Path) -> None:
    with pytest.raises(FloorFillConfigError) as refused:
        load_floor_fill_live_settings(
            live_section(tmp_path, plan_file_pattern="plan.md")
        )
    assert "plan_file_pattern" in str(refused.value)


def test_read_floor_returns_fetched_text(tmp_path: Path) -> None:
    source = facts(tmp_path, fetch_text=lambda url, timeout: f"{url}|{timeout}")
    assert source.read_floor("https://floor.example.invalid/f.json") == (
        "https://floor.example.invalid/f.json|5.0"
    )


def test_read_floor_unreadable_names_error_type(tmp_path: Path) -> None:
    def fail(url: str, timeout: float) -> str:
        raise TimeoutError

    with pytest.raises(FloorFillUnreadableError) as refused:
        facts(tmp_path, fetch_text=fail).read_floor("https://x.invalid/f.json")
    assert refused.value.cause == "TimeoutError"


def test_read_plan_joins_pattern_under_root(tmp_path: Path) -> None:
    (tmp_path / "alpha.md").write_text("plan text", encoding="utf-8")
    assert facts(tmp_path).read_plan(str(tmp_path), "alpha") == "plan text"


@pytest.mark.parametrize("line_id", ["../x", "a/b", "", "a\\b", "{line_id}"])
def test_read_plan_refuses_traversal(tmp_path: Path, line_id: str) -> None:
    calls: list[str] = []

    def record(path: str) -> str:
        calls.append(path)
        return ""

    with pytest.raises(FloorFillUnreadableError) as refused:
        facts(tmp_path, read_file=record).read_plan(str(tmp_path), line_id)
    assert refused.value.cause == "plan-path"
    assert calls == []


def test_read_plan_absent_file_is_unreadable(tmp_path: Path) -> None:
    with pytest.raises(FloorFillUnreadableError) as refused:
        facts(tmp_path).read_plan(str(tmp_path), "missing")
    assert refused.value.cause == "plan-absent"


def test_unbound_brake_and_stop_fail_closed(tmp_path: Path) -> None:
    source = facts(tmp_path)
    with pytest.raises(FloorFillUnreadableError) as brake:
        source.landing_only()
    assert brake.value.cause == "unbound:brake"
    with pytest.raises(FloorFillUnreadableError) as stop:
        source.stop_refuses("floor-task")
    assert stop.value.cause == "unbound:stop-signal"


def test_bound_brake_and_stop_are_read(tmp_path: Path) -> None:
    source = facts(tmp_path, brake=lambda: True, stop=lambda task_class: task_class)
    assert source.landing_only() is True
    assert source.stop_refuses("floor-task") == "floor-task"


def test_fetch_url_text_refuses_other_scheme() -> None:
    with pytest.raises(ValueError, match="scheme"):
        fetch_url_text("ftp://example.invalid/f", 1.0)


def test_fetch_url_text_reads_file_url(tmp_path: Path) -> None:
    target = tmp_path / "floor.json"
    target.write_text('{"a": 1}', encoding="utf-8")
    assert fetch_url_text(target.as_uri(), 1.0) == '{"a": 1}'


def test_ledger_rows_for_lane_exact_cell(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.md"
    ledger.write_text(
        "T | CLAIM | lane=a | key=k\nT | CLAIM | lane=ab | key=k\nT | TERMINAL | lane=a\n",
        encoding="utf-8",
    )
    store = FileWorkLedgerStore(str(ledger))
    assert store.rows_for_lane("a") == (
        "T | CLAIM | lane=a | key=k",
        "T | TERMINAL | lane=a",
    )
    assert store.queued_rows("a", "run") == ()


def test_ledger_floor_ledger_finds_claim_and_unread_file_raises(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.md"
    ledger.write_text("T | CLAIM | lane=L | key=K\n", encoding="utf-8")
    floor_ledger = LedgerRowsFloorLedger(FileWorkLedgerStore(str(ledger)))
    assert floor_ledger.claim_found("L", "K") is True
    assert floor_ledger.claim_found("L", "other") is False
    missing = LedgerRowsFloorLedger(FileWorkLedgerStore(str(tmp_path / "none.md")))
    with pytest.raises(FloorFillUnreadableError):
        missing.claim_found("L", "K")


def test_adapters_satisfy_the_spi_protocols(tmp_path: Path) -> None:
    assert isinstance(facts(tmp_path), ProtocolFactSource)
    assert isinstance(FileWorkLedgerStore(str(tmp_path / "l")), ProtocolWorkLedgerStore)


def test_live_profile_missing_setting_names_key(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    overlay = full_overlay(tmp_path)
    del overlay["floor_fill_live"]["ledger_path"]
    code = main(
        [
            "--overlay-json",
            json.dumps(overlay),
            "--this-host",
            "fill-host",
            "--profile",
            "live",
            "tick",
        ]
    )
    out = capsys.readouterr().out
    assert code == FAILED
    assert out.startswith("UNKNOWN fill-state:overlay:")
    assert "floor_fill_live.ledger_path" in out


def test_live_profile_dsn_unset_names_variable_not_value(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EXAMPLE_FLOOR_FILL_DSN", raising=False)
    code = main(
        [
            "--overlay-json",
            json.dumps(full_overlay(tmp_path)),
            "--this-host",
            "fill-host",
            "--profile",
            "live",
            "tick",
        ]
    )
    assert code == FAILED
    assert capsys.readouterr().out == (
        "UNKNOWN fill-state:store-dsn-unset:EXAMPLE_FLOOR_FILL_DSN\n"
    )


def test_unknown_profile_is_refused_naming_it(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = main(
        ["--overlay-json", "{}", "--this-host", "h", "--profile", "other", "tick"]
    )
    assert code == FAILED
    assert capsys.readouterr().out.startswith(
        "UNKNOWN fill-state:profile-unbound:other"
    )


def tick(tmp_path: Path, fetch: Any) -> tuple[str, int, InMemoryFloorStateStore]:
    from omnimarket.nodes.node_floor_fill_orchestrator.container.container_live import (
        build_live_handler_factory,
    )

    store = InMemoryFloorStateStore()
    settings = load_floor_fill_live_settings(live_section(tmp_path))
    (tmp_path / "ledger.md").write_text("", encoding="utf-8")
    (tmp_path / "out").mkdir()
    result = run_entry(
        ["tick"],
        {"floor_fill": OVERLAY},
        host_id=OVERLAY["host_id"],
        hosts=[(OVERLAY["host_id"], True)],
        build_handler=build_live_handler_factory(
            settings, dsn="unused", store=store, fetch_text=fetch
        ),
    )
    return result.stdout, result.exit_code, store


def test_live_tick_pool_unbound_fails_closed(tmp_path: Path) -> None:
    record = floor_record(LINES4, generated_at=T0)
    out, code, store = tick(tmp_path, lambda _url, _timeout: record)
    assert "pool-unread:unbound:pool" in out
    assert code == DEGRADED
    assert store.load("floor-fill-test").version == 0


def test_live_tick_floor_unreadable(tmp_path: Path) -> None:
    def fail(url: str, timeout: float) -> str:
        raise TimeoutError

    out, code, store = tick(tmp_path, fail)
    assert "floor-unread:TimeoutError" in out
    assert code == DEGRADED
    assert store.load("floor-fill-test").version == 0


def test_unbound_pool_is_not_read_as_zero_slots(tmp_path: Path) -> None:
    """Failing control: a pool that answered 0 would end the pass OK with no starts, which hides an unbound seam."""
    from omnimarket.nodes.node_floor_fill_orchestrator.orchestration.unbound_ports import (
        UnboundFloorPool,
    )

    with pytest.raises(FloorFillUnreadableError) as refused:
        UnboundFloorPool().free_slots()
    assert refused.value.cause == "unbound:pool"


def test_floor_record_item_states_are_the_producers() -> None:
    """Seam with node_factory_floor_compute: the fill reads every item state the floor can publish.

    The live record on the fill host carried ``integrating`` items, which the fill's own copy of the state list did
    not name, so the whole record read as malformed. The reader now takes the producer's ``ItemState``.
    """
    import typing

    from omnimarket.models.floor_fill.model_floor_fill_candidates import (
        ModelRecordItem,
    )
    from omnimarket.models.model_factory_floor_input import ItemState

    reader = ModelRecordItem.model_fields["state"].annotation
    assert set(typing.get_args(reader)) == set(typing.get_args(ItemState))


def test_integrating_item_is_not_startable() -> None:
    from omnimarket.models.floor_fill.model_floor_fill_candidates import (
        ModelFloorRecordRead,
    )

    record = json.loads(floor_record({"alpha": ["a1"]}, generated_at=T0))
    record["lines"][0]["stations"][0]["items"][0]["state"] = "integrating"
    parsed = ModelFloorRecordRead.model_validate_json(json.dumps(record))
    assert parsed.lines[0].stations[0].items[0].state == "integrating"
