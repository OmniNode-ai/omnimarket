# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23282: the closer ranks by when a ticket was last examined and holds an unchanged rejection.

An unchanged rejection posts no new closer comment, so the order on the newest comment's time
never moved and every fire picked the same unflippable tickets (7 of the 12 in the 05:45Z run
were CRITERIA_DRIFT). The selection now records each ticket it picks. Golden chain: fires over a
fixed scope rotate through every ticket, and the bundle command writes and reads the memory.
Error chain: an unreadable memory is refused by name, never an empty memory.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from omnimarket.handlers.closeout_examined_memory import (
    read_examined_memory,
    write_examined_memory,
)
from omnimarket.models.closeout_scope_overlay import (
    ModelCloseoutScopeOverlay,
)
from omnimarket.nodes.node_dod_closeout_sweep_compute.handlers.closer_examined_rules import (
    state_key,
)
from omnimarket.nodes.node_dod_closeout_sweep_compute.handlers.closer_scope_overlay import (
    load_scope_overlay,
)
from omnimarket.nodes.node_dod_closeout_sweep_compute.handlers.closer_scope_rules import (
    ScopeInputUnreadableError,
    scope_bundle,
    scope_examined_memory,
)
from omnimarket.nodes.node_dod_closeout_sweep_compute.runtime_dod_closeout_sweep import (
    main as runtime_main,
)

pytestmark = pytest.mark.unit

COMMENT = "2026-11-08T05:45:00.000Z"
SHA = "0123456789abcdef"


def _overlay(hold_after: int) -> ModelCloseoutScopeOverlay:
    return load_scope_overlay().model_copy(
        update={"hold_after_unchanged_selections": hold_after}
    )


def _scope(
    n: int, *, comment: str = COMMENT, waiting: dict[str, list[str]] | None = None
) -> tuple[list[Any], list[Any], list[Any]]:
    ids = [f"OMN-{i:02d}" for i in range(1, n + 1)]
    candidates = [{"id": i, "state": "In Review", "sources": ["started"]} for i in ids]
    prs = [{"id": i, "merged": ["omnimarket#1"], "open": [], "error": ""} for i in ids]
    text = [
        {
            "id": i,
            "text_sha": SHA,
            "last_closeout_comment_at": comment,
            "held_text_sha": "",
            "waiting_on": (waiting or {}).get(i, []),
        }
        for i in ids
    ]
    return candidates, prs, text


def _selected(
    scope: tuple[list[Any], list[Any], list[Any]],
    limit: int,
    overlay: ModelCloseoutScopeOverlay,
    memory: dict[str, dict[str, Any]],
) -> list[str]:
    bundle = scope_bundle(*scope, None, limit, overlay, examined=memory)
    return [c["id"] for c in bundle["candidates"]]


def _fire(
    scope: tuple[list[Any], list[Any], list[Any]],
    limit: int,
    overlay: ModelCloseoutScopeOverlay,
    memory: dict[str, dict[str, Any]],
    now: str,
) -> tuple[list[str], dict[str, dict[str, Any]]]:
    picked = _selected(scope, limit, overlay, memory)
    return picked, scope_examined_memory(*scope, None, limit, overlay, memory, now)


def test_closer_rank_last_examined_rotates_through_unchanged_tickets() -> None:
    """Six tickets, a bound of two per fire, no new comment ever: each fire takes the two least
    recently examined, so three fires cover all six. The oldest-comment order took OMN-01 and
    OMN-02 on every fire."""
    overlay = _overlay(hold_after=99)
    scope = _scope(6)
    memory: dict[str, dict[str, Any]] = {}
    seen: list[list[str]] = []
    for minute in range(3):
        picked, memory = _fire(
            scope, 2, overlay, memory, f"2026-11-09T06:0{minute}:00.000Z"
        )
        seen.append(picked)
    assert seen == [["OMN-01", "OMN-02"], ["OMN-03", "OMN-04"], ["OMN-05", "OMN-06"]]
    picked, memory = _fire(scope, 2, overlay, memory, "2026-11-09T06:03:00.000Z")
    assert picked == ["OMN-01", "OMN-02"]


def test_closer_rank_last_examined_never_examined_ranks_before_examined() -> None:
    overlay = _overlay(hold_after=99)
    scope = _scope(3)
    memory = {
        "OMN-01": {"examined_at": "2026-11-09T06:00:00.000Z", "key": "", "streak": 0}
    }
    assert _selected(scope, 2, overlay, memory) == ["OMN-02", "OMN-03"]


def test_closer_rank_last_examined_a_newer_comment_counts_as_an_examination() -> None:
    overlay = _overlay(hold_after=99)
    candidates, prs, text = _scope(3)
    text[0]["last_closeout_comment_at"] = "2026-11-09T07:00:00.000Z"
    assert _selected((candidates, prs, text), 2, overlay, {}) == ["OMN-02", "OMN-03"]


def test_closer_rank_last_examined_holds_a_ticket_whose_rejection_is_unchanged() -> (
    None
):
    overlay = _overlay(hold_after=1)
    scope = _scope(3)
    picked, memory = _fire(scope, 3, overlay, {}, "2026-11-09T06:00:00.000Z")
    assert picked == ["OMN-01", "OMN-02", "OMN-03"]
    bundle = scope_bundle(*scope, None, 3, overlay, examined=memory)
    assert bundle["candidates"] == []
    assert bundle["held"] == ["OMN-01", "OMN-02", "OMN-03"]
    assert bundle["totals"]["held"] == 3


@pytest.mark.parametrize(
    ("change", "released"),
    [("description", True), ("new_comment", True), ("none", False)],
)
def test_closer_rank_last_examined_hold_ends_when_the_state_changes(
    change: str, released: bool
) -> None:
    overlay = _overlay(hold_after=1)
    candidates, prs, text = _scope(1)
    _, memory = _fire(
        (candidates, prs, text), 1, overlay, {}, "2026-11-09T06:00:00.000Z"
    )
    if change == "description":
        text[0]["text_sha"] = "fedcba9876543210"
    elif change == "new_comment":
        text[0]["last_closeout_comment_at"] = "2026-11-09T06:30:00.000Z"
    bundle = scope_bundle(candidates, prs, text, None, 1, overlay, examined=memory)
    assert [c["id"] for c in bundle["candidates"]] == (["OMN-01"] if released else [])
    assert bundle["held"] == ([] if released else ["OMN-01"])


def test_closer_rank_last_examined_a_contract_appearing_ends_the_hold() -> None:
    overlay = _overlay(hold_after=1)
    scope = _scope(1)
    without = [{"id": "OMN-01", "has_contract": False}]
    memory = scope_examined_memory(
        *scope, without, 1, overlay, {}, "2026-11-09T06:00:00.000Z"
    )
    still = scope_bundle(*scope, without, 1, overlay, examined=memory)
    assert still["held"] == ["OMN-01"]
    with_contract = [{"id": "OMN-01", "has_contract": True}]
    now = scope_bundle(*scope, with_contract, 1, overlay, examined=memory)
    assert [c["id"] for c in now["candidates"]] == ["OMN-01"]
    assert now["held"] == []


def test_closer_rank_last_examined_waiting_on_and_never_commented_are_not_held() -> (
    None
):
    overlay = _overlay(hold_after=1)
    candidates, prs, text = _scope(3, waiting={"OMN-01": ["release cut"]})
    text[1]["last_closeout_comment_at"] = ""
    scope = (candidates, prs, text)
    _, memory = _fire(scope, 3, overlay, {}, "2026-11-09T06:00:00.000Z")
    bundle = scope_bundle(*scope, None, 3, overlay, examined=memory)
    assert [c["id"] for c in bundle["candidates"]] == ["OMN-01", "OMN-02"]
    assert bundle["held"] == ["OMN-03"]


def test_closer_rank_last_examined_state_key_is_empty_without_a_closer_comment() -> (
    None
):
    assert (
        state_key({"id": "OMN-1", "text_sha": SHA, "last_closeout_comment_at": ""})
        == ""
    )


def test_closer_rank_last_examined_memory_forgets_tickets_that_left_the_scope() -> None:
    overlay = _overlay(hold_after=99)
    memory = {
        "OMN-99": {"examined_at": "2026-11-09T05:00:00.000Z", "key": "k", "streak": 1}
    }
    _, after = _fire(_scope(2), 1, overlay, memory, "2026-11-09T06:00:00.000Z")
    assert sorted(after) == ["OMN-01"]


def _write(path: Path, rows: list[Any]) -> Path:
    path.write_text(json.dumps(rows), encoding="utf-8")
    return path


def _command(tmp_path: Path, *extra: str) -> list[str]:
    candidates, prs, text = _scope(3)
    return [
        "bundle",
        "--candidates", str(_write(tmp_path / "c.json", candidates)),
        "--pr-state", str(_write(tmp_path / "p.json", prs)),
        "--text-state", str(_write(tmp_path / "t.json", text)),
        "--contract-state", str(_write(tmp_path / "k.json", [])),
        "--max", "2",
        "--examined-memory", str(tmp_path / "memory.json"),
        *extra,
    ]  # fmt: skip


def test_closer_rank_last_examined_bundle_command_records_and_rotates(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    packaged = (
        Path(__file__).resolve().parents[3]
        / "src/omnimarket/nodes/node_dod_closeout_sweep_compute/overlay.yaml"
    )
    overlay_file = tmp_path / "overlay.yaml"
    overlay_file.write_text(
        packaged.read_text(encoding="utf-8").replace(
            "hold_after_unchanged_selections: 1", "hold_after_unchanged_selections: 99"
        ),
        encoding="utf-8",
    )
    args = _command(tmp_path, "--overlay", str(overlay_file))
    assert runtime_main([*args, "--now", "2026-11-09T06:00:00.000Z"]) == 0
    first = json.loads(capsys.readouterr().out)
    assert [c["id"] for c in first["candidates"]] == ["OMN-01", "OMN-02"]
    recorded = read_examined_memory(tmp_path / "memory.json")
    assert sorted(recorded) == ["OMN-01", "OMN-02"]
    assert recorded["OMN-01"]["examined_at"] == "2026-11-09T06:00:00.000Z"
    assert runtime_main([*args, "--now", "2026-11-09T06:01:00.000Z"]) == 0
    second = json.loads(capsys.readouterr().out)
    assert [c["id"] for c in second["candidates"]] == ["OMN-03", "OMN-01"]


def test_closer_rank_last_examined_no_record_leaves_the_memory_alone(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        runtime_main(
            [*_command(tmp_path, "--no-record"), "--now", "2026-11-09T06:00:00.000Z"]
        )
        == 0
    )
    capsys.readouterr()
    assert not (tmp_path / "memory.json").exists()


def test_closer_rank_last_examined_the_packaged_overlay_holds_after_one_selection(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    contracts = [{"id": f"OMN-{i:02d}", "has_contract": True} for i in (1, 2, 3)]
    args = _command(
        tmp_path, "--contract-state", str(_write(tmp_path / "k3.json", contracts))
    )
    assert runtime_main([*args, "--max", "3", "--now", "2026-11-09T06:00:00.000Z"]) == 0
    assert len(json.loads(capsys.readouterr().out)["candidates"]) == 3
    assert runtime_main([*args, "--max", "3", "--now", "2026-11-09T06:01:00.000Z"]) == 0
    held = json.loads(capsys.readouterr().out)
    assert held["candidates"] == []
    assert held["held"] == ["OMN-01", "OMN-02", "OMN-03"]


@pytest.mark.parametrize("content", ["not json", "[1, 2]", '{"OMN-1": 3}'])
def test_closer_rank_last_examined_an_unreadable_memory_is_refused_by_name(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], content: str
) -> None:
    (tmp_path / "memory.json").write_text(content, encoding="utf-8")
    assert runtime_main(_command(tmp_path)) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "closeout-scope: REFUSED" in captured.err
    assert "memory.json" in captured.err
    with pytest.raises(ScopeInputUnreadableError):
        read_examined_memory(tmp_path / "memory.json")


def test_closer_rank_last_examined_the_contract_integration_payload_holds_one_and_returns_memory() -> (
    None
):
    """The payload the ticket contract's integration proof sends through `onex run-node`, run
    through the handler: OMN-1 recorded at its current state is held, OMN-2 selected, and the
    memory after the selection carries OMN-2 examined now."""
    import yaml

    from omnimarket.models.dod_closeout_sweep import (
        ModelDodCloseoutDecisionRequest,
    )
    from omnimarket.nodes.node_dod_closeout_sweep_compute.handlers.handler_dod_closeout_sweep import (
        HandlerDodCloseoutSweep,
    )

    contract = yaml.safe_load(
        (Path(__file__).resolve().parents[3] / "contracts/OMN-23282.yaml").read_text(
            encoding="utf-8"
        )
    )
    proof = next(i for i in contract["dod_evidence"] if "integration-proof" in i["id"])
    command = proof["checks"][0]["check_value"]
    payload = json.loads(command.split(" --input '")[1].split("' | ")[0])
    result = HandlerDodCloseoutSweep().handle(
        ModelDodCloseoutDecisionRequest.model_validate(payload)
    )
    assert result.scope_bundle is not None
    assert result.scope_bundle["held"] == ["OMN-1"]
    selected = result.scope_bundle["candidates"]
    assert isinstance(selected, list)
    assert [c["id"] for c in selected if isinstance(c, dict)] == ["OMN-2"]
    assert result.scope_examined_next is not None
    assert (
        result.scope_examined_next["OMN-2"]["examined_at"] == "2026-11-09T07:00:00.000Z"
    )
    assert (
        result.scope_examined_next["OMN-1"]["examined_at"] == "2026-11-09T06:00:00.000Z"
    )


def test_closer_rank_last_examined_a_memory_path_that_is_a_directory_is_refused(
    tmp_path: Path,
) -> None:
    with pytest.raises(ScopeInputUnreadableError, match=str(tmp_path)):
        read_examined_memory(tmp_path)


def test_closer_rank_last_examined_a_memory_that_cannot_be_written_is_refused(
    tmp_path: Path,
) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text(
        "a file where the memory's directory should be", encoding="utf-8"
    )
    with pytest.raises(ScopeInputUnreadableError):
        write_examined_memory(blocker / "memory.json", {"OMN-1": {"streak": 1}})
    assert not list(tmp_path.glob(".*.tmp"))


def test_closer_rank_last_examined_a_written_memory_reads_back_whole(
    tmp_path: Path,
) -> None:
    memory = {
        "OMN-1": {"examined_at": "2026-11-09T06:00:00.000Z", "key": "k", "streak": 2}
    }
    write_examined_memory(tmp_path / "deep" / "memory.json", memory)
    assert read_examined_memory(tmp_path / "deep" / "memory.json") == memory
    assert read_examined_memory(tmp_path / "absent.json") == {}


def test_closer_rank_last_examined_an_empty_pr_state_for_candidates_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The prs command exited 2 on an overlay key the checkout did not know, the run wrote its
    stdout as [], and every one of 6500 tickets read 'no PR state row' on four fires in a row:
    a zero-flip that looked like a working closer. An empty array for candidates is the reader's
    failure, never a scope with nothing to do."""
    empty = _write(tmp_path / "empty.json", [])
    assert runtime_main([*_command(tmp_path, "--pr-state", str(empty))]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "closeout-scope: REFUSED" in captured.err
    assert "PR reader failed" in captured.err
    assert not (tmp_path / "memory.json").exists()


def test_closer_rank_last_examined_pr_rows_for_candidates_are_read(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert runtime_main(_command(tmp_path)) == 0
    assert len(json.loads(capsys.readouterr().out)["candidates"]) == 2


def test_closer_rank_last_examined_no_candidates_and_no_pr_state_is_an_empty_scope(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    empty = _write(tmp_path / "empty.json", [])
    args = _command(tmp_path, "--candidates", str(empty), "--pr-state", str(empty))
    assert runtime_main(args) == 0
    assert json.loads(capsys.readouterr().out)["candidates"] == []
