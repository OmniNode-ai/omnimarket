# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The remote-lane runner's seam: new work waits at the limit, a fix lane and a host with no overlay do not.

Every repository name, lane and ticket id below is an invented fixture.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.handlers import rules_wip_admission as rules
from omnimarket.handlers.wip_admission_brief_gate import (
    CLAIMS_READER,
    dispatch_wip_refusal,
)
from omnimarket.protocols.protocol_ledger_rows_reader import ProtocolLedgerRowsReader
from tests.helpers.fake_ledger_rows import FakeLedgerRows
from tests.nodes.node_wip_admission_compute.test_wip_admission import (
    CONFIG,
    NOW,
    REPO_A,
    overlay_doc,
    pr,
    watcher,
)

pytestmark = pytest.mark.unit


def host(tmp_path: Path, *, prs: int) -> dict[str, str]:
    """A launching host: the overlay and a watcher state file with ``prs`` open PRs.

    The environment names no ledger file: the claims come through the injected rows reader.
    """
    tmp_path.mkdir(parents=True, exist_ok=True)
    state = tmp_path / "watcher.json"
    state.write_text(
        json.dumps(watcher(*(pr(REPO_A, i) for i in range(1, prs + 1)))),
        encoding="utf-8",
    )
    doc = {**overlay_doc(), "watcher_state_path": str(state)}
    folder = tmp_path / "overlay" / "wip_admission"
    folder.mkdir(parents=True)
    (folder / "overlay.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")
    return {"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path / "overlay")}


def claim_rows(n: int) -> list[str]:
    """``n`` live CLAIM rows in ``REPO_A``: the work sent to lanes."""
    return [
        f"2026-10-30T11:{i:02d}:00Z | CLAIM | lane=fixture-l{i} | ticket=OMN-{i} | "
        f"worktree=omni_worktrees/OMN-{i}/{REPO_A} | est"
        for i in range(1, n + 1)
    ]


def full_host(tmp_path: Path) -> dict[str, str]:
    """A host whose repository is over its bound with lane claims (see ``FULL_LEDGER``)."""
    return host(tmp_path, prs=1)


FULL_LEDGER = claim_rows(11)


def ask(
    env: dict[str, str],
    ledger: Sequence[str] = (),
    reader: ProtocolLedgerRowsReader | None = None,
    **extra: Any,
) -> str | None:
    args: dict[str, Any] = {
        "lane": "fixture-lane",
        "repo": REPO_A,
        "ticket": "OMN-5",
        "prs": (),
        "environ": env,
        "now": NOW,
        "ledger_reader": reader if reader is not None else FakeLedgerRows(ledger),
    }
    return dispatch_wip_refusal(**{**args, **extra})


def test_new_work_is_refused_at_the_limit_and_admitted_under_it(tmp_path: Path) -> None:
    why = ask(full_host(tmp_path / "full"), FULL_LEDGER)
    assert why is not None
    assert why.startswith("REMOTE-LANE-WIP-REFUSED lane=fixture-lane REFUSE ")
    assert ask(host(tmp_path / "empty", prs=0)) is None


def test_94_open_pull_requests_do_not_refuse_a_dispatch(tmp_path: Path) -> None:
    """AC1 control (OMN-23028): the host's watcher state holds 94 open pull requests."""
    env = host(tmp_path, prs=94)
    assert ask(env) is None
    # The same host with lane claims over the bound is still refused (AC2).
    claims = host(tmp_path / "claims", prs=94)
    why = ask(claims, FULL_LEDGER)
    assert why is not None
    assert "reason=at_repo_limit" in why


def test_a_lane_working_a_named_pull_request_is_never_refused(tmp_path: Path) -> None:
    full = full_host(tmp_path)
    assert ask(full, FULL_LEDGER, prs=(f"fixture-org/{REPO_A}#1",)) is None


def test_a_lane_named_for_a_fix_is_never_refused(tmp_path: Path) -> None:
    assert ask(full_host(tmp_path), FULL_LEDGER, lane="fixture-cause-core-0000") is None


def test_the_watcher_state_path_is_read_with_the_environment_substituted(
    tmp_path: Path,
) -> None:
    env = full_host(tmp_path / "full")
    overlay = tmp_path / "full" / "overlay" / "wip_admission" / "overlay.yaml"
    doc = yaml.safe_load(overlay.read_text(encoding="utf-8"))
    doc["watcher_state_path"] = "${FIXTURE_ROOT}/watcher.json"
    overlay.write_text(yaml.safe_dump(doc), encoding="utf-8")
    assert ask({**env, "FIXTURE_ROOT": str(tmp_path / "full")}, FULL_LEDGER) is not None
    unset = ask(env, FULL_LEDGER)
    assert unset is not None
    assert "watcher state unread: KeyError" in unset


def test_a_host_with_no_overlay_judges_nothing(tmp_path: Path) -> None:
    assert ask({"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path / "none")}) is None


def test_an_unread_state_refuses_new_work_naming_why_and_admits_a_fix(
    tmp_path: Path,
) -> None:
    env = host(tmp_path, prs=0)
    Path(
        yaml.safe_load((tmp_path / "overlay/wip_admission/overlay.yaml").read_text())[
            "watcher_state_path"
        ]
    ).unlink()
    why = ask(env)
    assert why is not None
    assert "watcher state unread" in why
    assert ask(env, prs=(f"{REPO_A}#1",)) is None


def test_live_claims_from_the_ledger_are_the_count(tmp_path: Path) -> None:
    assert ask(host(tmp_path, prs=0), claim_rows(11)) is not None


def test_a_claim_ends_with_its_terminal_and_with_a_quiet_lane() -> None:
    open_row = "2026-10-30T11:00:00Z | CLAIM | lane=l1 | ticket=OMN-1 | repo=fixture-repo-a | x\n"
    closed = open_row + "2026-10-30T11:30:00Z | TERMINAL | lane=l1 | outcome=done\n"
    found = rules.claims_from_ledger(open_row, NOW, CONFIG)
    assert [(c.lane, c.ticket, c.repo) for c in found] == [
        ("l1", "OMN-1", "fixture-repo-a")
    ]
    assert rules.claims_from_ledger(closed, NOW, CONFIG) == []
    quiet = "2026-10-29T01:00:00Z | CLAIM | lane=l2 | ticket=OMN-2 | x\n"
    assert rules.claims_from_ledger(quiet, NOW, CONFIG) == []


def test_the_command_line_prints_the_refusal_and_exits_3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from omnimarket.nodes.node_wip_admission_compute import runtime_brief_gate as gate

    for key, value in full_host(tmp_path).items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(
        "omnimarket.handlers.wip_admission_brief_gate.SubprocessLedgerRowsReader",
        lambda _environ: FakeLedgerRows(FULL_LEDGER),
    )
    assert gate.main(["--lane", "fixture-lane", "--repo", REPO_A]) == gate.EXIT_REFUSE
    assert capsys.readouterr().out.startswith("REMOTE-LANE-WIP-REFUSED ")
    assert (
        gate.main(["--lane", "fixture-lane", "--repo", REPO_A, "--pr", f"{REPO_A}#1"])
        == gate.EXIT_ADMIT
    )


def test_the_claims_come_through_the_named_reader_and_window(tmp_path: Path) -> None:
    fake = FakeLedgerRows(FULL_LEDGER)
    assert ask(host(tmp_path, prs=0), reader=fake) is not None
    assert fake.calls == [(CLAIMS_READER, int(CONFIG.claim_window_hours), ())]
    assert CLAIMS_READER == "wip-admission-claims"


def test_no_ledger_file_is_opened_when_the_rows_come_from_the_reader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = host(tmp_path, prs=0)
    opened: list[str] = []
    real_read_text = Path.read_text

    def spy(self: Path, *args: Any, **kwargs: Any) -> str:
        opened.append(self.name)
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", spy)
    assert ask(env, claim_rows(1)) is None
    # Only the contract, the overlay and the watcher state: no file for the claims.
    assert sorted(set(opened)) == ["contract.yaml", "overlay.yaml", "watcher.json"]


def test_an_unreadable_ledger_refuses_new_work_naming_why_and_admits_a_fix(
    tmp_path: Path,
) -> None:
    env = host(tmp_path, prs=0)
    failing = FakeLedgerRows(error="reader exited 69: db down")
    why = ask(env, reader=failing)
    assert why is not None
    assert "REFUSE" in why
    assert "ledger unread: LedgerReadError" in why
    assert ask(env, reader=failing, prs=(f"{REPO_A}#1",)) is None


def test_the_default_reader_is_built_from_the_callers_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = host(tmp_path, prs=0)
    seen: list[object] = []

    def build(environ: object) -> FakeLedgerRows:
        seen.append(environ)
        return FakeLedgerRows(FULL_LEDGER)

    monkeypatch.setattr(
        "omnimarket.handlers.wip_admission_brief_gate.SubprocessLedgerRowsReader",
        build,
    )
    why = dispatch_wip_refusal(
        lane="fixture-lane",
        repo=REPO_A,
        ticket="OMN-5",
        prs=(),
        environ=env,
        now=NOW,
    )
    assert why is not None
    assert seen == [env]
