# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Negative controls and tick-to-dispatch proof for ruled work recovery."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from omnimarket.nodes.node_idle_slot_fill_effect.handlers.ruling_live_checks import (
    evaluate_check,
    packaged_checks,
)
from omnimarket.nodes.node_idle_slot_fill_effect.handlers.ruling_recovery import (
    detect_relaunches,
    parse_rows,
)

NOW = datetime(2026, 10, 26, 14, tzinfo=UTC)
RULING = "2026-10-20T21:56:19Z"


def ledger(*extra: str, claim: str = "", outcome: str = "blocked") -> str:
    return "\n".join(
        [
            f"{RULING} | RULING | kind=process | mechanism-ticket=OMN-123 | one runtime",
            f"2026-10-24T11:58:41Z | CLAIM | lane=fold | ticket=OMN-123 | repo=example | run=/runs/fold | {claim}",
            f"2026-10-24T12:52:12Z | TERMINAL | lane=fold | ticket=OMN-123 | outcome={outcome} | branch=omn-123-fold | patch=/runs/fold/staged.patch | blocker=hook-rounds-exhausted",
            *extra,
        ]
    )


@pytest.mark.parametrize("outcome", ["blocked", "partial"])
def test_blocked_ruled_work_carries_staged_artifacts(outcome: str) -> None:
    items, exceptions = detect_relaunches(parse_rows(ledger(outcome=outcome)), NOW)
    assert not exceptions
    assert len(items) == 1
    item = items[0]
    assert item["ticket"] == "OMN-123"
    assert item["required_engine"] == "codex"
    for value in [
        "omn-123-fold",
        "/runs/fold/staged.patch",
        "/runs/fold",
        "hook-rounds-exhausted",
        RULING,
    ]:
        assert value in item["brief"]


def test_waits_two_hours_and_later_claim_suppresses_old_terminal() -> None:
    rows = parse_rows(ledger())
    assert not detect_relaunches(rows, datetime(2026, 10, 24, 14, 52, 11, tzinfo=UTC))[
        0
    ]
    assert detect_relaunches(rows, datetime(2026, 10, 24, 14, 52, 12, tzinfo=UTC))[0]
    for stamp in ["2026-10-24T13:00:00Z", "2026-10-26T13:00:00Z"]:
        assert not detect_relaunches(
            parse_rows(ledger(f"{stamp} | CLAIM | lane=new | ticket=OMN-123")), NOW
        )[0]


@pytest.mark.parametrize("cite", ["claim", "brief"])
def test_claim_or_brief_citation_admits_non_mechanism_ticket(cite: str) -> None:
    text = ledger(
        claim=f"RULING {RULING}" if cite == "claim" else "brief=/brief/fold.md"
    ).replace("mechanism-ticket=OMN-123", "mechanism-ticket=OMN-999")
    items, _ = detect_relaunches(
        parse_rows(text),
        NOW,
        briefs={"fold": f"RULING {RULING}"} if cite == "brief" else {},
    )
    assert len(items) == 1


def test_unruled_success_and_superseded_terminal_are_not_relaunched() -> None:
    assert not detect_relaunches(
        parse_rows(ledger().replace("kind=process", "kind=product")), NOW
    )[0]
    assert not detect_relaunches(parse_rows(ledger(outcome="done")), NOW)[0]
    assert not detect_relaunches(
        parse_rows(
            ledger(
                "2026-10-24T13:00:00Z | TERMINAL | lane=fold | ticket=OMN-123 | outcome=done"
            )
        ),
        NOW,
    )[0]


def test_second_block_is_an_exception_even_without_headroom() -> None:
    items, _ = detect_relaunches(parse_rows(ledger()), NOW)
    sent = {items[0]["key"]: {"lane": "retry", "at": "2026-10-25T00:00:00Z"}}
    text = ledger(
        "2026-10-25T00:00:01Z | CLAIM | lane=retry | ticket=OMN-123",
        "2026-10-25T01:00:00Z | TERMINAL | lane=retry | ticket=OMN-123 | outcome=blocked | blocker=guard-refused",
    )
    found, exceptions = detect_relaunches(parse_rows(text), NOW, sent=sent)
    assert not found
    assert len(exceptions) == 1
    assert exceptions[0]["ticket"] == "OMN-123"
    assert "guard-refused" in exceptions[0]["blocker"]


def test_sent_item_is_not_repeated_after_dispatch_window() -> None:
    items, _ = detect_relaunches(parse_rows(ledger()), NOW)
    assert not detect_relaunches(
        parse_rows(ledger()),
        NOW,
        sent={items[0]["key"]: {"lane": "retry", "at": "2026-10-24T15:00:00Z"}},
    )[0]


def test_packaged_checks_declare_both_rulings() -> None:
    checks = packaged_checks()
    assert {c["ruling"] for c in checks} == {RULING, "2026-10-18T15:06:49Z"}


def test_runtime_check_counts_per_project_and_rejects_other_services() -> None:
    check = {
        "ruling": RULING,
        "kind": "compose-runtime",
        "runtime_services": ["runtime"],
        "backing_services": ["postgres"],
    }
    good = [
        {"project": "dev", "service": "runtime"},
        {"project": "dev", "service": "postgres"},
        {"project": "lab", "service": "runtime"},
    ]
    assert evaluate_check(check, good) is None
    for bad in [
        [*good, {"project": "dev", "service": "runtime"}],
        [*good, {"project": "dev", "service": "effects"}],
    ]:
        finding = evaluate_check(check, bad)
        assert finding
        assert finding[0] == "missing"
        assert finding[1] == f"ruling-drift:{RULING}"
        assert "dev" in finding[2]


def test_review_check_compares_exact_model_ids_not_voter_count() -> None:
    check = {
        "ruling": RULING,
        "kind": "review-voters",
        "model_ids": ["ruled-a", "ruled-b"],
    }
    assert evaluate_check(check, ["ruled-b", "ruled-a"]) is None
    finding = evaluate_check(check, ["ruled-a", "local-model"])
    assert finding
    assert finding[0] == "missing"
    assert "local-model" in finding[2]
    assert evaluate_check(check, ["ruled-a", "ruled-b", "ruled-b"])


def test_unread_live_state_never_matches() -> None:
    check = {"ruling": RULING, "kind": "review-voters", "model_ids": ["a", "b"]}
    finding = evaluate_check(check, RuntimeError("reader unavailable"))
    assert finding
    assert finding[0] == "missing"
    assert "unavailable" in finding[2]


def _tick(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    text: str,
    *,
    capacity: int = 1,
    read_only: bool = False,
    live_reader=None,
    record=None,
):
    from omnimarket.nodes.node_idle_slot_fill_effect.handlers.handler_idle_slot_fill import (
        HandlerLabFill,
    )
    from omnimarket.nodes.node_idle_slot_fill_effect.handlers.handler_ruling_recovery import (
        HandlerRulingRecovery,
    )
    from omnimarket.nodes.node_idle_slot_fill_effect.models import ModelLabFillRequest
    from tests.nodes.node_idle_slot_fill_effect.overlay_fixture import ENV

    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", ENV["ONEX_SKILL_OVERLAY_ROOTS"])
    monkeypatch.setenv("OMNI_LAB_FILL_GENERATE", "off")
    ledger_path = tmp_path / "ROLLING_WORK_LEDGER.md"
    ledger_path.write_text(text)
    monkeypatch.setenv("ONEX_LEDGER_PATH", str(ledger_path))
    backlog = tmp_path / "backlog.json"
    backlog.write_text(json.dumps({"items": []}))
    runner = tmp_path / "runner.py"
    runner.write_text(
        "import sys\nprint('DETACHED lane=' + sys.argv[sys.argv.index('--lane') + 1])\n"
    )
    monitor = HandlerRulingRecovery(
        live_reader or (lambda _check: RuntimeError("fixture unread"))
    )
    return HandlerLabFill(monitor).handle(
        ModelLabFillRequest(
            now="2026-10-26T14:00:00Z",
            request={
                "lab_headroom": {
                    "hosts": [
                        {
                            "name": "host-a",
                            "admission": {
                                "claude_slots": capacity,
                                "codex_slots": capacity,
                            },
                        }
                    ]
                }
            },
            lines=["MISSING lab-headroom:host-a | free slots"],
            launch=None,
            state_dir=tmp_path / "state",
            backlog=str(backlog),
            runner=runner,
            parent="orchestrator-test",
            out_of_scope={},
            read_only=read_only,
            record=record,
        )
    )


def test_tick_dispatches_codex_on_shared_host_and_keeps_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _tick(tmp_path, monkeypatch, ledger())
    assert any(
        f[1] == "lab-fill" and "sent=1" in f[2] and "codex=1" in f[2]
        for f in result.found
    )
    briefs = list((tmp_path / "state/lab-fill-briefs").glob("*.md"))
    assert len(briefs) == 1
    text = briefs[0].read_text()
    assert "ROUTE: model=codex" in text
    assert "omn-123-fold" in text
    assert "/runs/fold/staged.patch" in text
    assert "hook-rounds-exhausted" in text
    assert "never weaken a gate" in text
    # No second dispatch even when the ordinary dispatch window has expired.
    again = _tick(tmp_path, monkeypatch, ledger())
    assert any(f[1] == "lab-fill" and "sent=0" in f[2] for f in again.found)


def test_live_reading_changes_are_read_each_tick_without_headroom(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reading = ["local-model"]
    calls = []

    def reader(check):
        calls.append(check["ruling"])
        return (
            reading
            if check["kind"] == "review-voters"
            else [{"project": "dev", "service": "runtime"}]
        )

    before = _tick(tmp_path, monkeypatch, ledger(), capacity=0, live_reader=reader)
    assert any(
        f[1] == "ruling-drift:2026-10-18T15:06:49Z" and "local-model" in f[2]
        for f in before.found
    )
    reading[:] = next(
        c["model_ids"] for c in packaged_checks() if c["kind"] == "review-voters"
    )
    after = _tick(tmp_path, monkeypatch, ledger(), capacity=0, live_reader=reader)
    assert not any(f[1].startswith("ruling-drift:") for f in after.found)
    assert len(calls) == 4


def test_tick_records_one_orchestrator_exception_and_no_third_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _tick(tmp_path, monkeypatch, ledger())
    saved = json.loads((tmp_path / "state/lab-fill.sent.json").read_text())
    lane = next(iter(saved.values()))["lane"]
    text = ledger(
        f"2026-10-26T10:00:00Z | CLAIM | lane={lane} | ticket=OMN-123",
        f"2026-10-26T11:00:00Z | TERMINAL | lane={lane} | ticket=OMN-123 | outcome=blocked | blocker=still-refused",
    )
    records = []

    def record(*args):
        records.append(args)
        return

    for _ in range(2):
        result = _tick(tmp_path, monkeypatch, text, capacity=0, record=record)
        assert any(f[1] == "ruling-relaunch-exception:OMN-123" for f in result.found)
    rows = json.loads((tmp_path / "state/ruling-relaunch.exceptions.json").read_text())
    assert len(rows) == len(records) == 1
    assert next(iter(rows.values()))["owner"] == "orchestrator-test"
    assert next(iter(rows.values()))["action"] == "EXCEPTION"
    assert len(json.loads((tmp_path / "state/lab-fill.sent.json").read_text())) == 1


def test_read_only_tick_reads_checks_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _tick(tmp_path, monkeypatch, ledger(), read_only=True)
    assert any(f[1].startswith("ruling-drift:") for f in result.found)
    assert not (tmp_path / "state").exists()


def test_process_ruling_can_declare_one_live_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    declaration = '2026-10-25T00:00:00Z | RULING | kind=process | live-check={"kind":"review-voters","model_ids":["ruled-a"]}'
    result = _tick(
        tmp_path,
        monkeypatch,
        ledger(declaration),
        capacity=0,
        live_reader=lambda _c: ["wrong"],
    )
    assert any(
        f[1] == "ruling-drift:2026-10-25T00:00:00Z" and "wrong" in f[2]
        for f in result.found
    )


def test_ruling_in_an_archive_and_citation_in_a_brief_are_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = tmp_path / "archive"
    archive.mkdir()
    (archive / "ROLLING_WORK_LEDGER_2026-10-20-split.md").write_text(
        f"{RULING} | RULING | kind=process | mechanism-ticket=OMN-999"
    )
    briefs = tmp_path / "briefs"
    briefs.mkdir()
    (briefs / "fold.brief.md").write_text(f"Implement RULING {RULING}")
    monkeypatch.setenv("ONEX_LANE_BRIEF_DIRS", str(briefs))
    text = "\n".join(ledger().splitlines()[1:])
    result = _tick(tmp_path, monkeypatch, text)
    assert any(f[1] == "lab-fill" and "sent=1" in f[2] for f in result.found)


def test_shared_capacity_is_consumed_once_and_codex_reservations_hold_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnimarket.nodes.node_idle_slot_fill_effect.handlers.handler_idle_slot_fill import (
        HandlerIdleSlotFill,
    )
    from omnimarket.nodes.node_idle_slot_fill_effect.models import (
        ModelIdleSlotFillRequest,
    )
    from tests.nodes.node_idle_slot_fill_effect.overlay_fixture import ENV

    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", ENV["ONEX_SKILL_OVERLAY_ROOTS"])
    items, _ = detect_relaunches(parse_rows(ledger()), NOW)
    for reserved in [0, 1]:
        request = ModelIdleSlotFillRequest(
            now="2026-10-26T14:00:00Z",
            hosts=[
                {"name": "host-a", "admission": {"claude_slots": 1, "codex_slots": 1}}
            ],
            lines=["MISSING lab-headroom:host-a | free slots"],
            generated=items,
            placing={"codex": reserved},
        )
        result = HandlerIdleSlotFill().handle(request)
        assert len(result.sends) == 1 - reserved
        assert result.idle_after == 0


def test_packaged_node_deploy_behavior() -> None:
    from importlib.metadata import entry_points

    points = {e.name: e for e in entry_points(group="onex.nodes")}
    node = points["node_idle_slot_fill_effect"].load()
    assert node is not None
    checks = {c["kind"]: c for c in packaged_checks()}
    split = [
        {"project": "dev", "service": "runtime"},
        {"project": "dev", "service": "runtime-effects"},
    ]
    assert evaluate_check(checks["compose-runtime"], split)
    assert evaluate_check(checks["review-voters"], ["wrong-reviewer"])


def test_corrupt_dispatch_history_prevents_duplicate_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    (state / "lab-fill.sent.json").write_text("{broken")
    result = _tick(tmp_path, monkeypatch, ledger())
    assert any(f[0] == "unknown" and f[1] == "lab-fill-sent" for f in result.found)
    assert not (state / "lab-fill-briefs").exists()


def test_actual_local_run_directory_and_staged_patch_are_carried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "runs"
    (root / "rlane-fold").mkdir(parents=True)
    scratch = root / "rlane-fold.ctx/scratch"
    scratch.mkdir(parents=True)
    patch = scratch / "fold-staged.patch"
    patch.write_text("staged diff")
    monkeypatch.setenv("ONEX_REMOTE_LANE_RUN_ROOT", str(root))
    text = (
        ledger()
        .replace("run=/runs/fold", "run=rlane-fold")
        .replace(" | patch=/runs/fold/staged.patch", "")
    )
    _tick(tmp_path, monkeypatch, text)
    brief = next((tmp_path / "state/lab-fill-briefs").glob("*.md")).read_text()
    assert str(root / "rlane-fold") in brief
    assert str(patch) in brief


def test_review_live_reader_reads_overlay_and_served_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from io import BytesIO

    from omnimarket.nodes.node_idle_slot_fill_effect.handlers import (
        ruling_live_checks as checks,
    )

    path = tmp_path / "voters.yaml"
    path.write_text(
        "backend_overlay: backend.yaml\nvoters:\n  - voter_id: first\n    required: true\n    provider: anthropic\n    model: ruled-a\n  - voter_id: second\n    required: true\n    backend_id: local\n"
    )
    (tmp_path / "backend.yaml").write_text(
        "backends:\n  - backend_id: local\n    endpoint_url: http://voter.example.invalid:9000/v1/chat/completions\n"
    )
    urls = []

    def open_models(request, timeout):
        urls.append((request.full_url, timeout))
        return BytesIO(b'{"data":[{"id":"local-model"}]}')

    monkeypatch.setattr(checks.urllib.request, "urlopen", open_models)
    assert checks._review_reading({"REVIEW_VOTERS_OVERLAY": str(path)}) == [
        "ruled-a",
        "local-model",
    ]
    assert urls == [("http://voter.example.invalid:9000/v1/models", 3)]
    path.write_text(
        "voters:\n  - voter_id: first\n    required: true\n    provider: anthropic\n    model: ruled-a\n  - voter_id: second\n    required: true\n    provider: anthropic\n    model: ruled-b\n"
    )
    assert checks._review_reading({"REVIEW_VOTERS_OVERLAY": str(path)}) == [
        "ruled-a",
        "ruled-b",
    ]


def test_compose_live_reader_uses_live_container_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subprocess

    from omnimarket.nodes.node_idle_slot_fill_effect.handlers import (
        ruling_live_checks as checks,
    )

    calls = []

    def run(argv, **kw):
        calls.append((argv, kw))
        return subprocess.CompletedProcess(
            argv,
            0,
            json.dumps(
                {
                    "Labels": "com.docker.compose.project=dev,com.docker.compose.service=runtime"
                }
            )
            + "\n",
        )

    monkeypatch.setattr(checks.subprocess, "run", run)
    assert checks._compose_reading() == [{"project": "dev", "service": "runtime"}]
    assert calls[0][0][:2] == ["docker", "ps"]
    assert calls[0][1]["timeout"] == 15
    assert calls[0][1]["check"]


def test_corrected_mechanism_ticket_is_used() -> None:
    text = ledger().replace("mechanism-ticket=OMN-123", "mechanism-pending=yes")
    text += f"\n2026-10-21T00:00:00Z | CORRECTION | corrects={RULING} | mechanism-ticket=OMN-123"
    items, _ = detect_relaunches(parse_rows(text), NOW)
    assert len(items) == 1


def test_stored_recovery_kind_cannot_skip_stored_row_gates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnimarket.nodes.node_idle_slot_fill_effect.handlers.handler_idle_slot_fill import (
        HandlerIdleSlotFill,
    )
    from omnimarket.nodes.node_idle_slot_fill_effect.models import (
        ModelIdleSlotFillRequest,
    )
    from tests.nodes.node_idle_slot_fill_effect.overlay_fixture import ENV

    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", ENV["ONEX_SKILL_OVERLAY_ROOTS"])
    items, _ = detect_relaunches(parse_rows(ledger()), NOW)
    result = HandlerIdleSlotFill().handle(
        ModelIdleSlotFillRequest(
            now="2026-10-26T14:00:00Z",
            hosts=[
                {"name": "host-a", "admission": {"claude_slots": 1, "codex_slots": 1}}
            ],
            lines=["MISSING lab-headroom:host-a | free slots"],
            backlog=items,
        )
    )
    assert not result.sends
    assert len(result.dropped) == 1


def test_second_block_survives_exception_queue_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnimarket.nodes.node_idle_slot_fill_effect.handlers import (
        handler_ruling_recovery as recovery,
    )

    def refuse(*_args, **_kwargs):
        raise OSError("queue unwritable")

    monkeypatch.setattr(recovery, "_record_exceptions", refuse)
    text = ledger(
        "2026-10-25T00:00:00Z | CLAIM | lane=retry | ticket=OMN-123",
        "2026-10-25T01:00:00Z | TERMINAL | lane=retry | ticket=OMN-123 | outcome=blocked | blocker=guard-refused",
    )
    result = _tick(tmp_path, monkeypatch, text, capacity=0)
    assert any(f[1] == "ruling-relaunch-exception:OMN-123" for f in result.found)
    assert any(
        f[0] == "unknown" and f[1] == "ruling-relaunch-exceptions" for f in result.found
    )


def test_claim_in_terminal_second_is_still_a_later_claim() -> None:
    text = ledger("2026-10-24T12:52:12Z | CLAIM | lane=retry | ticket=OMN-123")
    assert not detect_relaunches(parse_rows(text), NOW)[0]


def test_unknown_recovery_repository_is_held_at_the_scope_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _tick(tmp_path, monkeypatch, ledger().replace(" | repo=example", ""))
    assert any(f[1] == "ruling-relaunch-scope:OMN-123" for f in result.found)
    assert not (tmp_path / "state/lab-fill-briefs").exists()


def test_owning_repository_is_read_from_the_original_brief() -> None:
    text = ledger().replace(" | repo=example", "")
    items, _ = detect_relaunches(
        parse_rows(text),
        NOW,
        briefs={"fold": f"Repository: owner/example\nImplement RULING {RULING}"},
    )
    assert items[0]["repo"] == "example"
