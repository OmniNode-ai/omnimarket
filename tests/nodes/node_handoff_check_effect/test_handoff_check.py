# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for handoff_check.py . Recorded fixtures only: no live service is read."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

from omnimarket.nodes.node_handoff_check_effect import runtime_handoff_check_cli as cli
from omnimarket.nodes.node_handoff_check_effect.handlers.handler_handoff_check import (
    CONTRACT_PATH,
    load_policy,
)
from omnimarket.nodes.node_handoff_check_effect.handlers.ledger_rows import (
    load_rows,
    parse_row,
)
from omnimarket.nodes.node_handoff_check_effect.handlers.rulings import (
    dropped_decisions,
)
from omnimarket.nodes.node_handoff_check_effect.models import (
    ModelFact,
    ModelLedgerRow,
    parse_ts,
)

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).resolve().parent / "fixtures"

NOW = "2026-11-01T23:00:00Z"
RULING = (
    '2026-11-01T20:00:00Z | RULING | lane=orch | ticket=TKT-1 | question=planted | "go"'
)
CLAIM_OTHER = (
    "2026-11-01T20:30:00Z | CLAIM | lane=other | ticket=TKT-9 | actor=x | model=x"
)


def umbrella_claims(ticket: str = "TKT-U", lanes: int = 20) -> list[str]:
    """Enough distinct lanes claiming one ticket to make it an umbrella."""
    return [
        f"2026-11-01T10:{n:02d}:00Z | CLAIM | lane=u{n} | ticket={ticket} | actor=x | model=x"
        for n in range(lanes)
    ]


def write_ledger(tmp_path: Path, *rows: str) -> Path:
    path = tmp_path / "ledger.md"
    path.write_text("# ledger\n" + "\n".join(rows) + "\n", encoding="utf-8")
    return path


def write_probes(tmp_path: Path, *recs: dict) -> Path:
    path = tmp_path / "probes.json"
    path.write_text(json.dumps(list(recs)), encoding="utf-8")
    return path


def probe(
    name: str, healthy: bool | None, read_at: str = NOW, detail: str = "recorded"
) -> dict:
    return {
        "name": name,
        "healthy": healthy,
        "detail": detail,
        "read_at": read_at,
        "probe": f"recorded {name}",
    }


def run(tmp_path: Path, ledger: Path, probes: Path, *extra: str, now: str = NOW) -> int:
    argv = [
        "assemble",
        "--ledger",
        str(ledger),
        "--archive-dir",
        str(tmp_path / "none"),
        "--now",
        now,
        "--probes",
        str(probes),
        *extra,
    ]
    return cli.main(argv)


def test_policy_is_the_handoff_check_section_of_the_node_contract() -> None:
    raw = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    policy = load_policy()
    assert policy.contract_version == raw["handoff_check"]["contract_version"]
    assert policy.ruling_gate.statuses == ("done", "in_flight", "dropped", "held")
    assert len(policy.probes) == len(raw["handoff_check"]["probes"])


def test_contract_declares_every_required_section() -> None:
    ids = [s.id for s in load_policy().sections]
    assert ids == [
        "session",
        "goal",
        "plans",
        "rulings",
        "holds",
        "lanes",
        "stopped",
        "asks",
        "prs",
        "limits",
        "services",
        "unverified",
    ]


def test_planted_ruling_without_status_refuses_the_handoff(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger = write_ledger(tmp_path, RULING, CLAIM_OTHER)
    out_dir = tmp_path / "out"
    rc = run(
        tmp_path,
        ledger,
        write_probes(tmp_path, probe("dev-bus", True)),
        "--out-dir",
        str(out_dir),
    )
    out = capsys.readouterr().out
    assert rc == 1
    assert "HANDOFF REFUSED: 1 item(s) unaccounted" in out
    assert "UNACCOUNTED 2026-11-01T20:00:00Z RULING ticket=TKT-1" in out
    assert not out_dir.exists()


@pytest.mark.parametrize(
    ("record", "reason"),
    [
        (
            {"status": "finished", "evidence": "x"},
            "is not one of done, in_flight, dropped, held",
        ),
        ({"status": "done", "evidence": ""}, "no evidence"),
    ],
)
def test_invalid_declared_status_refuses(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], record: dict, reason: str
) -> None:
    ledger = write_ledger(tmp_path, RULING)
    statuses = tmp_path / "statuses.json"
    statuses.write_text(json.dumps({"2026-11-01T20:00:00Z": record}), encoding="utf-8")
    assert (
        run(tmp_path, ledger, write_probes(tmp_path), "--statuses", str(statuses)) == 1
    )
    assert reason in capsys.readouterr().out


def test_declared_status_accepts_and_writes_the_handoff(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger = write_ledger(tmp_path, RULING)
    statuses = tmp_path / "statuses.json"
    statuses.write_text(
        json.dumps(
            {
                "2026-11-01T20:00:00Z": {
                    "status": "dropped",
                    "evidence": "operator said stop",
                }
            }
        ),
        encoding="utf-8",
    )
    assert (
        run(
            tmp_path,
            ledger,
            write_probes(tmp_path),
            "--statuses",
            str(statuses),
            "--out-dir",
            str(tmp_path / "out"),
            "--session",
            "s1",
        )
        == 0
    )
    written = list((tmp_path / "out").glob("*-s1-handoff.md"))
    assert len(written) == 1
    assert "status=dropped (declared); evidence: operator said stop" in written[
        0
    ].read_text(encoding="utf-8")
    assert "HANDOFF OK" in capsys.readouterr().out


def test_status_is_derived_from_an_open_claim_on_the_ruling_ticket(
    tmp_path: Path,
) -> None:
    claim = (
        "2026-11-01T20:10:00Z | CLAIM | lane=builder | ticket=TKT-1 | actor=x | model=x"
    )
    ledger = write_ledger(tmp_path, RULING, claim)
    assert (
        run(
            tmp_path, ledger, write_probes(tmp_path), "--out-dir", str(tmp_path / "out")
        )
        == 0
    )
    assert "status=in_flight (derived from the ledger)" in next(
        (tmp_path / "out").iterdir()
    ).read_text(encoding="utf-8")


def test_umbrella_ticket_never_derives_a_status(tmp_path: Path) -> None:
    ruling = RULING.replace("TKT-1", "TKT-U")
    claim = (
        "2026-11-01T20:10:00Z | CLAIM | lane=builder | ticket=TKT-U | actor=x | model=x"
    )
    ledger = write_ledger(tmp_path, *umbrella_claims(), ruling, claim)
    assert run(tmp_path, ledger, write_probes(tmp_path)) == 1


def test_unhealthy_service_is_recorded_and_never_refuses(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger = write_ledger(
        tmp_path,
        "2026-11-01T20:30:00Z | CLAIM | lane=other | ticket=TKT-9 | actor=x | model=x",
    )
    probes = write_probes(
        tmp_path,
        probe("dev-bus", False, detail="ConnectionRefusedError"),
        probe(
            "runtime-health",
            None,
            detail="unconfigured: ONEX_RUNTIME_HEALTH_URL is not set",
        ),
    )
    assert run(tmp_path, ledger, probes, "--out-dir", str(tmp_path / "out")) == 0
    doc = next((tmp_path / "out").iterdir()).read_text(encoding="utf-8")
    assert (
        "dev-bus: UNHEALTHY (ConnectionRefusedError) [src: probe recorded dev-bus; read: 2026-11-01T23:00:00Z"
        in doc
    )
    assert "service dev-bus is UNHEALTHY at 2026-11-01T23:00:00Z" in doc
    assert "runtime-health: unconfigured" in doc
    assert "HANDOFF OK" in capsys.readouterr().out


def test_stale_read_is_marked_stale_and_listed_unverified(tmp_path: Path) -> None:
    ledger = write_ledger(tmp_path, CLAIM_OTHER)
    probes = write_probes(
        tmp_path,
        probe("dev-bus", True, read_at="2026-11-01T22:00:00Z"),
        probe("ledger", True, read_at="2026-11-01T22:59:00Z"),
    )
    assert run(tmp_path, ledger, probes, "--out-dir", str(tmp_path / "out")) == 0
    doc = next((tmp_path / "out").iterdir()).read_text(encoding="utf-8")
    stale_line = next(
        ln for ln in doc.splitlines() if ln.startswith("- dev-bus: healthy")
    )
    fresh_line = next(
        ln for ln in doc.splitlines() if ln.startswith("- ledger: healthy")
    )
    assert stale_line.endswith("; STALE]")
    assert "STALE" not in fresh_line
    assert "STALE read: dev-bus: healthy" in doc


def fact(read_at: str, ttl: int) -> ModelFact:
    return ModelFact(text="x", source="s", read_at=read_at, ttl=ttl)


def test_stale_age_comes_from_the_contract() -> None:
    assert load_policy().stale.ttl_for("probe") == 600
    now = parse_ts(NOW)
    assert fact("2026-11-01T22:49:59Z", 600).stale(now)
    assert not fact("2026-11-01T22:50:01Z", 600).stale(now)
    assert not fact("2020-01-01T00:00:00Z", 0).stale(now)


def test_every_factual_line_carries_source_and_read_time(tmp_path: Path) -> None:
    ledger = write_ledger(
        tmp_path,
        RULING,
        "2026-11-01T20:10:00Z | CLAIM | lane=b | ticket=TKT-1 | actor=x | model=x",
    )
    assert (
        run(
            tmp_path,
            ledger,
            write_probes(tmp_path, probe("dev-bus", True)),
            "--out-dir",
            str(tmp_path / "out"),
            "--goal-file",
            str(_goal(tmp_path)),
        )
        == 0
    )
    doc = next((tmp_path / "out").iterdir()).read_text(encoding="utf-8")
    fact_lines = [ln for ln in doc.splitlines() if ln.startswith("- ")]
    assert len(fact_lines) >= 8
    for line in fact_lines:
        fact = ModelFact.parse_line(line)
        assert fact is not None, line
        assert fact.source, line
        assert fact.read_at, line


def _goal(tmp_path: Path) -> Path:
    path = tmp_path / "goal.txt"
    path.write_text("make the handoff a contract\n", encoding="utf-8")
    return path


def dropped_ledger(tmp_path: Path, *rows: str) -> list[ModelLedgerRow]:
    contract_rows = [parse_row(r, "t") for r in rows]
    return sorted((r for r in contract_rows if r), key=lambda r: r.ts)


@pytest.mark.parametrize(
    ("rows", "bucket"),
    [
        (
            ['2026-10-31T10:00:00Z | RULING | lane=o | ticket=TKT-1 | "go"'],
            "dropped",
        ),
        (
            [
                '2026-10-31T10:00:00Z | RULING | lane=o | ticket=TKT-1 | "go"',
                "2026-10-31T12:00:00Z | CLAIM | lane=b | ticket=TKT-1 | actor=x",
            ],
            "evidenced",
        ),
        (
            [
                '2026-10-31T10:00:00Z | RULING | lane=o | ticket=TKT-1 | "go"',
                "2026-10-31T12:00:00Z | TERMINAL | lane=b | ticket=TKT-1 | outcome=done",
            ],
            "evidenced",
        ),
        (
            [
                '2026-10-31T10:00:00Z | RULING | lane=o | ticket=TKT-1 | "go"',
                "2026-11-01T11:00:00Z | CLAIM | lane=b | ticket=TKT-1 | actor=x",
            ],
            "dropped",
        ),
        (
            ['2026-10-31T10:00:00Z | RULING | lane=o | ticket=TKT-U | "go"'],
            "unlinkable",
        ),
        (
            [
                '2026-10-31T10:00:00Z | RULING | lane=o | ticket=TKT-U | "go"',
                "2026-10-31T13:00:00Z | CLAIM | lane=b | ticket=TKT-U | consent=2026-10-31T10:00:00Z",
            ],
            "evidenced",
        ),
    ],
)
def test_dropped_count_rule(tmp_path: Path, rows: list[str], bucket: str) -> None:
    policy = load_policy()
    parsed = dropped_ledger(
        tmp_path,
        *rows,
        *umbrella_claims(),
        "2026-11-01T22:00:00Z | STATUS | lane=z | note=newest row",
    )
    result = dropped_decisions(parsed, policy, parse_ts("2026-11-01T22:30:00Z"))
    assert [
        len(getattr(result, k)) for k in ("dropped", "evidenced", "unlinkable")
    ] == [int(bucket == k) for k in ("dropped", "evidenced", "unlinkable")]


def test_dropped_count_pending_inside_the_horizon(tmp_path: Path) -> None:
    policy = load_policy()
    parsed = dropped_ledger(
        tmp_path,
        '2026-11-01T20:00:00Z | RULING | lane=o | ticket=TKT-1 | "go"',
        "2026-11-01T22:00:00Z | STATUS | lane=z | note=newest",
    )
    result = dropped_decisions(parsed, policy, parse_ts("2026-11-01T22:30:00Z"))
    assert len(result.pending_no_evidence_yet) == 1
    assert not result.dropped


def test_dropped_count_reproduces_on_the_recorded_slice_of_2026_11_01(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    expected = json.loads(
        (FIXTURES / "expected_dropped.json").read_text(encoding="utf-8")
    )
    ledger = FIXTURES / "ledger_slice_2026-11-01.md"
    assert run(tmp_path, ledger, write_probes(tmp_path), now=expected["now"]) in (0, 1)
    first = capsys.readouterr().out.splitlines()[0]
    assert first == (
        f"DROPPED-DECISIONS dropped={expected['dropped']} judged={expected['judged']} unlinkable={expected['unlinkable']} "
        f"pending_no_evidence_yet={expected['pending_no_evidence_yet']} pending_evidenced={expected['pending_evidenced']}"
    )
    rows = load_rows(ledger, None)
    result = dropped_decisions(rows, load_policy(), parse_ts(expected["now"]))
    assert [d.stamp for d in result.dropped] == expected["dropped_items"]


def test_fixture_holds_no_free_text_or_paths() -> None:
    text = (FIXTURES / "ledger_slice_2026-11-01.md").read_text(encoding="utf-8")
    assert "/Users" + "/" not in text
    assert "http" not in text
    assert "OMN-" not in text
    assert all(ln.endswith("text=[scrubbed]") for ln in text.splitlines())


def make_kb_clone(tmp_path: Path, files: dict[str, str]) -> Path:
    repo = tmp_path / "kb"
    repo.mkdir()

    def git(*a: str) -> None:
        subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "-c",
                "user.name=t",
                "-c",
                "user.email=t@example.com",
                *a,
            ],
            check=True,
            capture_output=True,
            env=scrub_git_location_env(os.environ),
        )

    git("init", "-q", "-b", "main")
    for name, body in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(body, encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "handoffs")
    git("update-ref", "refs/remotes/origin/main", "HEAD")
    (repo / "beta/handoffs").mkdir(parents=True, exist_ok=True)
    (repo / "beta/handoffs/2099-01-01T0000Z-worktree-only-handoff.md").write_text(
        "not committed\n", encoding="utf-8"
    )
    return repo


OLD = "# Session handoff old\n\ncontract: handoff_contract.yaml v1; assembled 2026-11-01T10:00:00Z; ledger x\n\n## Holds\n\n- stale text [src: s; read: 2026-11-01T09:00:00Z; ttl: 3600; val: x]\n"
NEW = (
    "# Session handoff new\n\ncontract: handoff_contract.yaml v1; assembled 2026-11-01T21:00:00Z; ledger x\n\n## PR state\n\n"
    "- r#1: state=OPEN ci=PENDING [src: pr_state_local.py; read: 2026-11-01T21:00:00Z; ttl: 900; probe: pr:r#1; val: state=OPEN ci=PENDING]\n"
    "- r#2: state=OPEN ci=GREEN [src: pr_state_local.py; read: 2026-11-01T21:00:00Z; ttl: 900; probe: pr:r#2; val: state=OPEN ci=GREEN]\n"
    "- r#3: state=MERGED ci=GREEN [src: pr_state_local.py; read: 2026-11-01T21:00:00Z; ttl: 0; probe: pr:r#3; val: state=MERGED ci=GREEN]\n"
    "- note without a probe [src: writer; read: 2026-11-01T21:00:00Z; ttl: 600]\n"
)


def test_resume_reads_the_newest_committed_handoff_and_lists_a_changed_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    clone = make_kb_clone(
        tmp_path,
        {
            "beta/handoffs/2026-11-01T1000Z-a-handoff.md": OLD,
            "beta/handoffs/2026-11-01T2100Z-b-handoff.md": NEW,
        },
    )
    ledger = write_ledger(
        tmp_path, '2026-11-01T22:00:00Z | RULING | lane=o | ticket=TKT-1 | "go"'
    )
    recorded = tmp_path / "reprobe.json"
    recorded.write_text(
        json.dumps(
            {"pr:r#1": "state=MERGED ci=GREEN", "pr:r#2": "state=OPEN ci=GREEN"}
        ),
        encoding="utf-8",
    )
    rc = cli.main(
        [
            "resume",
            "--kb-clone",
            str(clone),
            "--ledger",
            str(ledger),
            "--archive-dir",
            str(tmp_path / "none"),
            "--now",
            NOW,
            "--probes",
            str(recorded),
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "newest handoff beta/handoffs/2026-11-01T2100Z-b-handoff.md" in out
    assert "CHANGED pr:r#1: state=OPEN ci=PENDING -> state=MERGED ci=GREEN" in out
    assert "UNCHANGED pr:r#2: state=OPEN ci=GREEN" in out
    assert "r#3" not in out
    assert "worktree-only" not in out
    assert "NEW since handoff: 1 RULING/OPERATOR-CONSENT/HOLD row(s)" in out
    assert (
        "RESUME SUMMARY changed=1 unchanged=1 unreadable=0 stale_without_probe=1" in out
    )


def test_resume_with_no_handoff_exits_3(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    clone = make_kb_clone(tmp_path, {"README.md": "x\n"})
    ledger = write_ledger(tmp_path, CLAIM_OTHER)
    assert (
        cli.main(
            [
                "resume",
                "--kb-clone",
                str(clone),
                "--ledger",
                str(ledger),
                "--now",
                NOW,
                "--probes",
                str(write_probes(tmp_path)),
            ]
        )
        == 3
    )
    assert "no handoff under beta/handoffs" in capsys.readouterr().out
